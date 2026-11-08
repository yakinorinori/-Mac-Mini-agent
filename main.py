import asyncio
import os
import tempfile
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from approvals import DEFAULT_TTL_SECONDS
from config import ALLOWED_TELEGRAM_USER_IDS, TOKEN
from agent.agent import Agent
from agent.learned_rules import MIN_SUCCESSES, WINDOW_DAYS, normalize_key
from agent.executor import ToolManager
from agent.schemas import plan_requires_approval
from context import Context
from heartbeat import INTERVAL_SECONDS, current_commit, state_dir, write_heartbeat
from jobs import JobQueue
from scheduler import Scheduler
from store import StoredApprovals, loggable_text, open_store
import voice
from tools.vscode.send_chat import open_dev_mode, type_into_vscode_chat, submit_vscode_chat

tm = ToolManager()
app_context = Context()
agent = Agent(app_context, registry=tm.registry, rule_lookup=lambda text: store.learned_level(text))
scheduler = Scheduler()
store = open_store(state_dir())
approvals = StoredApprovals(store)
jobs = JobQueue()
scheduled_timers = {}

# 既定の待ち時間では、画像の処理中に応答待ちが切れることがある
PHOTO_TIMEOUT_SECONDS = 60

# Telegramの1メッセージの上限(4096文字)より少し小さく区切る
MAX_MESSAGE_LENGTH = 4000

LABEL_LENGTH = 30

# 停止中にこれより長く実行時刻を過ぎた予約は、古すぎるので実行しない
LATE_LIMIT_SECONDS = 60 * 60

# 音声メモの上限。長い録音は文字起こしに時間がかかり、他の依頼を待たせるため
MAX_VOICE_SECONDS = 120

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


def build_voice_filter(allowed_user_ids):
    """音声メモを受け付けるフィルタを作る。"""

    return build_user_filter(allowed_user_ids) & filters.VOICE


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


class ChatReplier:
    """再起動後に元のメッセージがなくても、同じチャットへ返信できるようにする。"""

    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    async def reply_text(self, text, **kwargs):
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def reply_photo(self, photo, **kwargs):
        return await self.bot.send_photo(chat_id=self.chat_id, photo=photo, **kwargs)


def _rule_key(plan, text):
    return normalize_key(loggable_text(text, plan))


async def forget_learned_rule(plan, text, message):
    """学習ルールで振り分けた依頼が失敗や中止になったら、そのルールを外す。"""

    if not plan.route.startswith("learned:"):
        return

    key = _rule_key(plan, text)

    if key and store.remove_rule(key=key):
        await message.reply_text(f"🧠 学習ルール「{key}」を外しました。次からは通常の判定に戻します。")


async def maybe_propose_rule(plan, text, message):
    """LLMの判定で同じモデルが続けて成功した依頼を、ルールとして提案する。"""

    if not plan.route.startswith("classify:"):
        return

    key = _rule_key(plan, text)
    level = store.rule_candidate(key, time.time() - WINDOW_DAYS * 86400, MIN_SUCCESSES)

    if level is None:
        return

    rule_id = store.propose_rule(key, level)

    if rule_id is None:
        return

    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 追加", callback_data=f"rule_yes:{rule_id}"),
        InlineKeyboardButton("🚫 追加しない", callback_data=f"rule_no:{rule_id}"),
    ]])
    await message.reply_text(
        f"🧠 「{key}」は直近{WINDOW_DAYS}日に {plan.model} で{MIN_SUCCESSES}回以上成功しました。\n"
        f"次から難易度の判定を省き、{plan.model} で計画しますか？（承認が必要な操作は今までどおり確認します）",
        reply_markup=buttons,
    )


async def execute_and_record(plan, text="", message=None):
    """ツールを別スレッドで実行し、経路と結果を記録する。"""

    started = time.monotonic()

    try:
        results = await asyncio.to_thread(tm.execute_plan, plan)
    except Exception:
        store.record_run(plan, "failed", time.monotonic() - started, text)
        if message is not None:
            await forget_learned_rule(plan, text, message)
        raise

    store.record_run(plan, "succeeded", time.monotonic() - started, text)
    return results


async def run_delayed(plan, message, text=""):
    """遅延実行の処理本体。時刻になるとキューから呼ばれる。"""

    try:
        results = await execute_and_record(plan, text, message)
        await send_results(message, plan, results)
    except Exception as e:
        print("エラー:", type(e).__name__)
        await message.reply_text(f"⚠️ 遅延実行でエラーが発生しました: {e}")


def schedule_stored(job_id, delay, plan, message, owner, loop, text=""):
    """時刻になったらキューへ入れるタイマーを登録する。実行中の処理と重ならないようにするため。"""

    holder = {}

    def fire():
        scheduled_timers.pop(holder.get("timer"), None)

        # /cancel 済みなら実行しない
        if not store.claim_scheduled(job_id):
            return

        async def enqueue():
            jobs.submit("遅延実行", lambda: run_delayed(plan, message, text), owner)

        try:
            asyncio.run_coroutine_threadsafe(enqueue(), loop).result(timeout=60)
        except Exception as e:
            print("遅延実行の登録エラー:", type(e).__name__)

    timer = scheduler.schedule(max(0, delay), fire)

    if timer is not None:
        holder["timer"] = timer
        scheduled_timers[timer] = owner


async def run_plan(message, plan, text, owner):
    """Planを実行、または遅延実行を予約する。"""

    if plan.delay:
        job_id = store.add_scheduled(owner, message.chat_id, time.time() + plan.delay, text, plan)
        schedule_stored(job_id, plan.delay, plan, message, owner, asyncio.get_running_loop(), text)
        # この時点では未実行なので、LLMが用意した完了文面は返さない
        await message.reply_text(
            f"⏰ {plan.delay}秒後に実行します。結果はあとで通知します。/cancel で取り消せます。"
        )
    else:
        results = await execute_and_record(plan, text, message)
        await send_results(message, plan, results)
        await maybe_propose_rule(plan, text, message)

    app_context.add(
        text,
        plan
    )


async def restore_scheduled(bot):
    """再起動前の予約を戻す。実行時刻を大きく過ぎたものは実行せずに知らせる。"""

    loop = asyncio.get_running_loop()
    now = time.time()
    restored = {}
    dropped = {}

    for row in store.list_scheduled():
        late = now - row["run_at"]

        if late > LATE_LIMIT_SECONDS:
            store.claim_scheduled(row["id"])
            dropped[row["chat_id"]] = dropped.get(row["chat_id"], 0) + 1
            continue

        replier = ChatReplier(bot, row["chat_id"])
        schedule_stored(row["id"], row["run_at"] - now, row["plan"], replier, row["user_id"], loop, row["text"])
        restored[row["chat_id"]] = restored.get(row["chat_id"], 0) + 1

    for chat_id in set(restored) | set(dropped):
        lines = []

        if restored.get(chat_id):
            lines.append(f"⏰ 再起動前の予約を{restored[chat_id]}件引き継ぎました。/cancel で取り消せます。")

        if dropped.get(chat_id):
            lines.append(f"⚠️ 停止中に実行時刻を1時間以上過ぎた予約を{dropped[chat_id]}件取り消しました。")

        try:
            await bot.send_message(chat_id=chat_id, text="\n".join(lines))
        except Exception as e:
            print("通知エラー:", type(e).__name__)


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


async def handle_text(update: Update, text: str, from_voice=False):

    # ── 開発モード ──────────────────────────────
    # 操作の語(開発モード、送信、終了)は入力した文字だけで受け付ける。聞き間違いで送信しないため
    if not from_voice and text.strip() == "開発モード":
        await asyncio.to_thread(open_dev_mode)
        app_context.dev_mode = True
        await update.message.reply_text(
            "🛠 開発モードを開始しました。送るトークがそのままVS Codeのチャットに入力されます。"
            "「送信」で送信、「終了」でモードを終了します。"
        )
        return

    if app_context.dev_mode:

        if from_voice:
            await asyncio.to_thread(type_into_vscode_chat, text)
            await update.message.reply_text(
                "VS Codeのチャット欄に入力しました。送信と終了は文字で送ってください。"
            )
            return

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
        store.record_run(None, "not_understood", 0, text, route=getattr(agent.planner, "_route", "none"))
        await update.message.reply_text("分かりませんでした")
        return

    if not plan.steps:
        # 実行するものがない（対応不可やLLMの失敗）。返答だけ送る
        store.record_run(plan, "no_steps", 0, text)
        await update.message.reply_text(plan.message or "実行できる操作がありませんでした。")
        return

    if plan_requires_approval(plan, tm.registry):
        await request_approval(update, plan, text)
        return

    # LLMが選んだツールと操作フローをユーザーに提示する
    if plan.steps:
        await update.message.reply_text(plan.describe())

    await run_plan(update.message, plan, text, update.effective_user.id)


async def process_text(update: Update, text: str, from_voice=False):

    try:
        await handle_text(update, text, from_voice)
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


async def transcribe_message(message):
    """音声メモを一時フォルダへ保存して文字起こしし、フォルダごと消す。"""

    with tempfile.TemporaryDirectory(prefix="agent-voice-") as directory:
        audio = Path(directory) / "voice.oga"
        file = await message.voice.get_file()
        await file.download_to_drive(audio)

        return await asyncio.to_thread(voice.transcribe, audio, Path(directory))


async def process_voice(update: Update):

    problems = voice.setup_problems()

    if problems:
        await update.message.reply_text(
            "🎙 音声認識が未導入です: " + "、".join(problems)
            + "\nMac miniで python -m voice --check を実行して確認してください。"
        )
        return

    started = time.monotonic()

    try:
        text = await transcribe_message(update.message)
    except Exception as e:
        print("文字起こしエラー:", type(e).__name__)
        await update.message.reply_text(f"⚠️ 文字起こしに失敗しました: {e}")
        return

    seconds = time.monotonic() - started
    print(f"文字起こし: {len(text)}文字、{seconds:.1f}秒")

    if not text:
        await update.message.reply_text(f"🎙 聞き取れませんでした（{seconds:.1f}秒）。")
        return

    await update.message.reply_text(f"🎙 「{text}」（文字起こし {seconds:.1f}秒）")
    await process_text(update, text, from_voice=True)


async def receive_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """音声メモをキューへ入れる。文字起こしもLLMと同じく1件ずつ実行する。"""

    duration = update.message.voice.duration or 0

    if duration > MAX_VOICE_SECONDS:
        await update.message.reply_text(
            f"🎙 音声は{MAX_VOICE_SECONDS}秒までです（{duration}秒）。短く分けて送ってください。"
        )
        return

    print(f"受信: 音声{duration}秒")

    ahead = jobs.submit(
        f"音声メモ（{duration}秒）",
        lambda: process_voice(update),
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

    removed = store.cancel_scheduled(owner)

    if removed:
        cancelled += [f"遅延実行 {removed}件"]

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
        store.record_run(pending.plan, "rejected", 0, pending.text)
        await message.reply_text("🚫 中止しました。実行していません。")
        await forget_learned_rule(pending.plan, pending.text, message)
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
    directory = state_dir()

    if directory:
        name = os.getenv("AGENT_NAME", "prod")
        app.bot_data["heartbeat"] = asyncio.create_task(heartbeat_loop(directory, name))

    await restore_scheduled(app.bot)


async def _on_stop(app):
    task = app.bot_data.get("heartbeat")
    if task:
        task.cancel()

    jobs.close()

    for timer in list(scheduled_timers):
        timer.cancel()

    scheduled_timers.clear()


async def on_rule_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ルール提案と削除のボタンを処理する。"""

    query = update.callback_query

    if update.effective_user.id not in ALLOWED_TELEGRAM_USER_IDS:
        await query.answer()
        return

    await _acknowledge(query)
    action, _, raw_id = query.data.partition(":")
    rule_id = int(raw_id)

    if action == "rule_del":
        removed = store.remove_rule(rule_id=rule_id)
        await query.message.reply_text("🗑 ルールを削除しました。" if removed else "このルールはもうありません。")
        return

    decided = store.decide_rule(rule_id, accept=action == "rule_yes")

    if decided is None:
        await query.message.reply_text("この提案はすでに処理済みです。")
    elif action == "rule_yes":
        await query.message.reply_text(f"✅ 「{decided[0]}」のルールを追加しました。/rules で確認、削除できます。")
    else:
        await query.message.reply_text("🚫 追加しませんでした。この依頼は今後も提案しません。")


async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = store.active_rules()

    if not rules:
        await update.message.reply_text("学習ルールはありません。")
        return

    for rule in rules:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 削除", callback_data=f"rule_del:{rule['id']}"),
        ]])
        await update.message.reply_text(f"「{rule['key']}」→ {rule['level']}", reply_markup=buttons)


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
        MessageHandler(build_voice_filter(ALLOWED_TELEGRAM_USER_IDS), receive_voice)
    )
    app.add_handler(
        CommandHandler("cancel", cancel, filters=build_user_filter(ALLOWED_TELEGRAM_USER_IDS))
    )
    app.add_handler(
        CommandHandler("rules", show_rules, filters=build_user_filter(ALLOWED_TELEGRAM_USER_IDS))
    )
    app.add_handler(CallbackQueryHandler(on_rule_decision, pattern=r"^rule_(yes|no|del):[1-9][0-9]{0,9}$"))
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
