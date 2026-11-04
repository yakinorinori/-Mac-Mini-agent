import asyncio
import os

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from approvals import DEFAULT_TTL_SECONDS, ApprovalStore
from config import ALLOWED_TELEGRAM_USER_IDS, TOKEN
from agent.agent import Agent
from agent.executor import ToolManager
from agent.schemas import plan_requires_approval
from context import Context
from heartbeat import INTERVAL_SECONDS, current_commit, state_dir, write_heartbeat
from jobs import JobQueue
from scheduler import Scheduler
from tools.vscode.send_chat import open_dev_mode, type_into_vscode_chat, submit_vscode_chat

tm = ToolManager()
app_context = Context()
agent = Agent(app_context, registry=tm.registry)
scheduler = Scheduler()
approvals = ApprovalStore()
jobs = JobQueue()
scheduled_timers = {}

# 既定の待ち時間では、画像の処理中に応答待ちが切れることがある
PHOTO_TIMEOUT_SECONDS = 60

# Telegramの1メッセージの上限(4096文字)より少し小さく区切る
MAX_MESSAGE_LENGTH = 4000

LABEL_LENGTH = 30

APPROVE = "approve"
REJECT = "reject"


def build_user_filter(allowed_user_ids):
    """許可ユーザーからの個人チャットの新規メッセージだけを受け付けるフィルタを作る。"""

    return (
        filters.UpdateType.MESSAGE
        & filters.ChatType.PRIVATE
        & filters.User(user_id=allowed_user_ids)
    )


def build_message_filter(allowed_user_ids):
    """コマンドを除くテキストを受け付けるフィルタを作る。"""

    return build_user_filter(allowed_user_ids) & filters.TEXT & ~filters.COMMAND


async def send_results(message, plan, results):
    """実行結果をユーザーへ返す。take_screenshotの結果(画像パス)は画像として送信する。"""

    for step, result in results:
        if step.tool == "google_search" and result:
            await message.reply_text(result)

        if step.tool == "take_screenshot" and result:
            with open(result, "rb") as photo:
                try:
                    await message.reply_photo(
                        photo,
                        read_timeout=PHOTO_TIMEOUT_SECONDS,
                        write_timeout=PHOTO_TIMEOUT_SECONDS,
                    )
                except TimedOut:
                    # 届いている場合があるので、重複を避けて自動で再送しない
                    await message.reply_text(
                        "⚠️ 画像の送信完了を確認できませんでした。届いていなければ、もう一度送ってください。"
                    )
                    return

    await message.reply_text(plan.message or "完了しました。")


async def run_delayed(plan, message):
    """遅延実行の処理本体。時刻になるとキューから呼ばれる。"""

    try:
        results = await asyncio.to_thread(tm.execute_plan, plan)
        await send_results(message, plan, results)
    except Exception as e:
        print("エラー:", type(e).__name__)
        await message.reply_text(f"⚠️ 遅延実行でエラーが発生しました: {e}")


def _schedule_delayed(plan, message, owner, loop):
    """時刻になったらキューへ入れるタイマーを登録する。実行中の処理と重ならないようにするため。"""

    holder = {}

    def fire():
        scheduled_timers.pop(holder.get("timer"), None)

        async def enqueue():
            jobs.submit("遅延実行", lambda: run_delayed(plan, message), owner)

        try:
            asyncio.run_coroutine_threadsafe(enqueue(), loop).result(timeout=60)
        except Exception as e:
            print("遅延実行の登録エラー:", type(e).__name__)

    timer = scheduler.schedule(plan.delay, fire)

    if timer is not None:
        holder["timer"] = timer
        scheduled_timers[timer] = owner


async def run_plan(message, plan, text, owner):
    """Planを実行、または遅延実行を予約する。"""

    if plan.delay:
        _schedule_delayed(plan, message, owner, asyncio.get_running_loop())
        # この時点では未実行なので、LLMが用意した完了文面は返さない
        await message.reply_text(
            f"⏰ {plan.delay}秒後に実行します。結果はあとで通知します。/cancel で取り消せます。"
        )
    else:
        results = await asyncio.to_thread(tm.execute_plan, plan)
        await send_results(message, plan, results)

    app_context.add(
        text,
        plan
    )


async def request_approval(update: Update, plan, text):
    """実行内容をすべて表示し、実行または中止のボタンを送る。"""

    approval_id = approvals.add(plan, text, update.effective_user.id)
    preview = (
        f"{plan.describe()}\n\n"
        f"この内容で実行しますか？({DEFAULT_TTL_SECONDS // 60}分で無効になります)"
    )
    chunks = [
        preview[start:start + MAX_MESSAGE_LENGTH]
        for start in range(0, len(preview), MAX_MESSAGE_LENGTH)
    ]
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 実行", callback_data=f"{APPROVE}:{approval_id}"),
        InlineKeyboardButton("🚫 中止", callback_data=f"{REJECT}:{approval_id}"),
    ]])

    for chunk in chunks[:-1]:
        await update.message.reply_text(chunk)

    await update.message.reply_text(chunks[-1], reply_markup=buttons)


async def handle_text(update: Update, text: str):

    # ── 開発モード ──────────────────────────────
    if text.strip() == "開発モード":
        await asyncio.to_thread(open_dev_mode)
        app_context.dev_mode = True
        await update.message.reply_text(
            "🛠 開発モードを開始しました。送るトークがそのままVS Codeのチャットに入力されます。"
            "「送信」で送信、「終了」でモードを終了します。"
        )
        return

    if app_context.dev_mode:

        if text.strip() == "終了":
            app_context.dev_mode = False
            await update.message.reply_text("開発モードを終了しました。")
            return

        if text.strip() == "送信":
            await asyncio.to_thread(submit_vscode_chat)
            await update.message.reply_text("VS Codeのチャットに送信しました。")
            return

        await asyncio.to_thread(type_into_vscode_chat, text)
        await update.message.reply_text("VS Codeのチャット欄に入力しました。")
        return

    await update.message.reply_text("⏳ 処理中です。")

    plan = await asyncio.to_thread(agent.plan, text)

    if plan is None:
        await update.message.reply_text("分かりませんでした")
        return

    if plan_requires_approval(plan, tm.registry):
        await request_approval(update, plan, text)
        return

    # LLMが選んだツールと操作フローをユーザーに提示する
    if plan.steps:
        await update.message.reply_text(plan.describe())

    await run_plan(update.message, plan, text, update.effective_user.id)


async def process_text(update: Update, text: str):

    try:
        await handle_text(update, text)
    except Exception as e:
        print("エラー:", type(e).__name__)
        await update.message.reply_text(f"⚠️ エラーが発生しました: {e}")


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """処理をキューへ入れてすぐ戻る。実行中でも次のメッセージを受け付けるため。"""

    text = update.message.text

    print(f"受信: {len(text)}文字")

    ahead = jobs.submit(
        text[:LABEL_LENGTH],
        lambda: process_text(update, text),
        update.effective_user.id,
    )

    if ahead:
        await update.message.reply_text(
            f"📥 受け付けました（前に{ahead}件）。/cancel で未開始の処理を取り消せます。"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """未開始の処理と予約中の遅延実行を取り消す。実行中の処理は止められない。"""

    owner = update.effective_user.id
    cancelled = jobs.cancel_pending(owner)
    timers = [timer for timer, timer_owner in scheduled_timers.items() if timer_owner == owner]

    for timer in timers:
        timer.cancel()
        scheduled_timers.pop(timer, None)

    if timers:
        cancelled += [f"遅延実行 {len(timers)}件"]

    lines = [f"🚫 取り消しました: {label}" for label in cancelled] or ["取り消せる処理はありません。"]
    running = jobs.running_label(owner)

    if running:
        lines.append(f"実行中の処理は止められません: {running}")

    await update.message.reply_text("\n".join(lines))


async def _acknowledge(query):
    """ボタンの読み込み表示を消してボタンを外す。失敗しても判断の処理は続ける。"""

    try:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print("承認ボタンの更新エラー:", e)


async def _run_approved(message, pending, owner):
    try:
        await run_plan(message, pending.plan, pending.text, owner)
    except Exception as e:
        print("エラー:", type(e).__name__)
        await message.reply_text(f"⚠️ エラーが発生しました: {e}")


async def on_approval_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    user_id = update.effective_user.id

    if user_id not in ALLOWED_TELEGRAM_USER_IDS:
        await query.answer()
        return

    action, _, approval_id = query.data.partition(":")
    pending = approvals.take(approval_id, user_id)

    await _acknowledge(query)

    message = query.message

    if pending is None:
        await message.reply_text("この確認はすでに処理済みか、期限切れです。実行していません。")
        return

    if action != APPROVE:
        await message.reply_text("🚫 中止しました。実行していません。")
        return

    ahead = jobs.submit(
        f"承認済み: {pending.text[:LABEL_LENGTH]}",
        lambda: _run_approved(message, pending, user_id),
        user_id,
    )

    if ahead:
        await message.reply_text(
            f"📥 承認しました（前に{ahead}件）。/cancel で開始前なら取り消せます。"
        )


async def heartbeat_loop(directory, name):
    commit = await asyncio.to_thread(current_commit)

    while True:
        try:
            write_heartbeat(directory, name, commit)
        except OSError as e:
            print("heartbeatエラー:", e.strerror)

        await asyncio.sleep(INTERVAL_SECONDS)


async def _on_start(app):
    try:
        await app.bot.set_my_commands([BotCommand("cancel", "未開始の処理を取り消す")])
    except Exception as e:
        print("メニュー設定エラー:", type(e).__name__)

    directory = state_dir()

    if directory:
        name = os.getenv("AGENT_NAME", "prod")
        app.bot_data["heartbeat"] = asyncio.create_task(heartbeat_loop(directory, name))


async def _on_stop(app):
    task = app.bot_data.get("heartbeat")
    if task:
        task.cancel()

    jobs.close()

    for timer in list(scheduled_timers):
        timer.cancel()

    scheduled_timers.clear()


def build_application():

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_on_start)
        .post_shutdown(_on_stop)
        .build()
    )

    app.add_handler(
        MessageHandler(build_message_filter(ALLOWED_TELEGRAM_USER_IDS), receive)
    )
    app.add_handler(
        CommandHandler("cancel", cancel, filters=build_user_filter(ALLOWED_TELEGRAM_USER_IDS))
    )
    app.add_handler(
        CallbackQueryHandler(on_approval_decision, pattern=f"^({APPROVE}|{REJECT}):")
    )

    return app


def main():

    if not ALLOWED_TELEGRAM_USER_IDS:
        raise SystemExit(
            "ALLOWED_TELEGRAM_USER_IDS が未設定のため起動しません。"
            "操作を許可するTelegramユーザーIDを設定してください。"
        )

    app = build_application()

    print("Bot起動")

    app.run_polling()


if __name__ == "__main__":
    main()
