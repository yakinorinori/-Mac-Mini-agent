import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
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
from scheduler import Scheduler
from tools.vscode.send_chat import open_dev_mode, type_into_vscode_chat, submit_vscode_chat

tm = ToolManager()
app_context = Context()
agent = Agent(app_context, registry=tm.registry)
scheduler = Scheduler()
approvals = ApprovalStore()

# 既定の待ち時間では、画像の処理中に応答待ちが切れることがある
PHOTO_TIMEOUT_SECONDS = 60

# Telegramの1メッセージの上限(4096文字)より少し小さく区切る
MAX_MESSAGE_LENGTH = 4000

APPROVE = "approve"
REJECT = "reject"


def build_message_filter(allowed_user_ids):
    """許可ユーザーからの個人チャットの新規テキストだけを受け付けるフィルタを作る。"""

    return (
        filters.UpdateType.MESSAGE
        & filters.ChatType.PRIVATE
        & filters.TEXT
        & filters.User(user_id=allowed_user_ids)
    )


async def send_results(message, plan, results):
    """実行結果をユーザーへ返す。take_screenshotの結果(画像パス)は画像として送信する。"""

    for step, result in results:
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


def run_delayed(plan, message, loop):
    """Schedulerのスレッドで実行し、成否をbotのイベントループ経由で通知する。"""

    try:
        results = tm.execute_plan(plan)
        notification = send_results(message, plan, results)
    except Exception as e:
        print("エラー:", e)
        notification = message.reply_text(f"⚠️ 遅延実行でエラーが発生しました: {e}")

    future = asyncio.run_coroutine_threadsafe(notification, loop)

    try:
        future.result(timeout=60)
    except Exception as e:
        print("通知エラー:", e)


async def run_plan(message, plan, text):
    """Planを実行、または遅延実行を予約する。"""

    if plan.delay:
        loop = asyncio.get_running_loop()
        scheduler.schedule(
            plan.delay,
            lambda: run_delayed(plan, message, loop)
        )
        # この時点では未実行なので、LLMが用意した完了文面は返さない
        await message.reply_text(
            f"⏰ {plan.delay}秒後に実行します。結果はあとで通知します。"
        )
    else:
        results = tm.execute_plan(plan)
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
        open_dev_mode()
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
            submit_vscode_chat()
            await update.message.reply_text("VS Codeのチャットに送信しました。")
            return

        type_into_vscode_chat(text)
        await update.message.reply_text("VS Codeのチャット欄に入力しました。")
        return

    plan = agent.plan(text)

    if plan is None:
        await update.message.reply_text("分かりませんでした")
        return

    if plan_requires_approval(plan, tm.registry):
        await request_approval(update, plan, text)
        return

    # LLMが選んだツールと操作フローをユーザーに提示する
    if plan.steps:
        await update.message.reply_text(plan.describe())

    await run_plan(update.message, plan, text)


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    print("受信:", text)

    try:
        await handle_text(update, text)
    except Exception as e:
        print("エラー:", e)
        await update.message.reply_text(f"⚠️ エラーが発生しました: {e}")


async def _acknowledge(query):
    """ボタンの読み込み表示を消してボタンを外す。失敗しても判断の処理は続ける。"""

    try:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print("承認ボタンの更新エラー:", e)


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

    try:
        await run_plan(message, pending.plan, pending.text)
    except Exception as e:
        print("エラー:", e)
        await message.reply_text(f"⚠️ エラーが発生しました: {e}")


def build_application():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        MessageHandler(build_message_filter(ALLOWED_TELEGRAM_USER_IDS), receive)
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
