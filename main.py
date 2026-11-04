import asyncio

from telegram import Update
from telegram.error import TimedOut
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ALLOWED_TELEGRAM_USER_IDS, TOKEN
from agent.agent import Agent
from agent.executor import ToolManager
from context import Context
from scheduler import Scheduler
from tools.vscode.send_chat import open_dev_mode, type_into_vscode_chat, submit_vscode_chat

tm = ToolManager()
app_context = Context()
agent = Agent(app_context, registry=tm.registry)
scheduler = Scheduler()

# 既定の待ち時間では、画像の処理中に応答待ちが切れることがある
PHOTO_TIMEOUT_SECONDS = 60


def build_message_filter(allowed_user_ids):
    """許可ユーザーからの個人チャットの新規テキストだけを受け付けるフィルタを作る。"""

    return (
        filters.UpdateType.MESSAGE
        & filters.ChatType.PRIVATE
        & filters.TEXT
        & filters.User(user_id=allowed_user_ids)
    )


async def send_results(update: Update, plan, results):
    """実行結果をユーザーへ返す。take_screenshotの結果(画像パス)は画像として送信する。"""

    for step, result in results:
        if step.tool == "take_screenshot" and result:
            with open(result, "rb") as photo:
                try:
                    await update.message.reply_photo(
                        photo,
                        read_timeout=PHOTO_TIMEOUT_SECONDS,
                        write_timeout=PHOTO_TIMEOUT_SECONDS,
                    )
                except TimedOut:
                    # 届いている場合があるので、重複を避けて自動で再送しない
                    await update.message.reply_text(
                        "⚠️ 画像の送信完了を確認できませんでした。届いていなければ、もう一度送ってください。"
                    )
                    return

    await update.message.reply_text(plan.message or "完了しました。")


def run_delayed(plan, update: Update, loop):
    """Schedulerのスレッドで実行し、成否をbotのイベントループ経由で通知する。"""

    try:
        results = tm.execute_plan(plan)
        notification = send_results(update, plan, results)
    except Exception as e:
        print("エラー:", e)
        notification = update.message.reply_text(f"⚠️ 遅延実行でエラーが発生しました: {e}")

    future = asyncio.run_coroutine_threadsafe(notification, loop)

    try:
        future.result(timeout=60)
    except Exception as e:
        print("通知エラー:", e)


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

    # LLMが選んだツールと操作フローをユーザーに提示する
    if plan.steps:
        await update.message.reply_text(plan.describe())

    if plan.delay:
        loop = asyncio.get_running_loop()
        scheduler.schedule(
            plan.delay,
            lambda: run_delayed(plan, update, loop)
        )
        # この時点では未実行なので、LLMが用意した完了文面は返さない
        await update.message.reply_text(
            f"⏰ {plan.delay}秒後に実行します。結果はあとで通知します。"
        )
    else:
        results = tm.execute_plan(plan)
        await send_results(update, plan, results)

    app_context.add(
        text,
        plan
    )


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    print("受信:", text)

    try:
        await handle_text(update, text)
    except Exception as e:
        print("エラー:", e)
        await update.message.reply_text(f"⚠️ エラーが発生しました: {e}")


def main():

    if not ALLOWED_TELEGRAM_USER_IDS:
        raise SystemExit(
            "ALLOWED_TELEGRAM_USER_IDS が未設定のため起動しません。"
            "操作を許可するTelegramユーザーIDを設定してください。"
        )

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        MessageHandler(build_message_filter(ALLOWED_TELEGRAM_USER_IDS), receive)
    )

    print("Bot起動")

    app.run_polling()


if __name__ == "__main__":
    main()
