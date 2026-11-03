from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TOKEN
from agent.agent import Agent
from agent.executor import ToolManager
from context import Context
from scheduler import Scheduler
from tools.vscode.send_chat import open_dev_mode, type_into_vscode_chat, submit_vscode_chat

tm = ToolManager()
app_context = Context()
agent = Agent(app_context, registry=tm.registry)
scheduler = Scheduler()


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    print("受信:", text)

    # ── 開発モード ──────────────────────────────
    if text.strip() == "開発モード":
        app_context.dev_mode = True
        open_dev_mode()
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

    try:
        if plan.delay:

            scheduler.schedule(
                plan.delay,
                lambda: tm.execute_plan(plan)
            )

        else:

            results = tm.execute_plan(plan)

            # take_screenshotの結果(画像パス)はTelegramへ画像として送信する
            for step, result in results:
                if step.tool == "take_screenshot" and result:
                    with open(result, "rb") as photo:
                        await update.message.reply_photo(photo)

        await update.message.reply_text(
            plan.message
        )
        app_context.add(
            text,
            plan
        )

    except Exception as e:
        print("エラー:", e)
        await update.message.reply_text("エラーが発生しました")


app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(
    MessageHandler(filters.TEXT, receive)
)

print("Bot起動")

app.run_polling()
