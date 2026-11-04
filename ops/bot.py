"""運用専用のTelegram Bot。LLMやツールを読み込まず、固定のコマンドだけを受け付ける。"""
import asyncio
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from approvals import DEFAULT_TTL_SECONDS, ApprovalStore
from config import ALLOWED_TELEGRAM_USER_IDS, TOKEN
from heartbeat import current_commit, write_heartbeat
from ops import system

MONITOR_INTERVAL_SECONDS = 60
# 今の本番Botは計画中に処理が止まることがあるので、長めに判定する
HEARTBEAT_STALE_SECONDS = 300
MEMORY_FREE_ALERT_PERCENT = 10
DISK_FREE_ALERT_GB = 5
MAX_MESSAGE_LENGTH = 3500

APPROVE = "ops_approve"
REJECT = "ops_reject"

HELP = """運用コマンド:
/status 各プロセス、コミット、メモリ、ディスク、CI
/logs prod または /logs ops 直近のログ
/stop_prod 本番Botを停止（承認あり）
/start_prod 本番Botを再開（承認あり）
/restart prod または /restart ops 再起動（承認あり）"""

approvals = ApprovalStore()


def build_filter(allowed_user_ids):
    return filters.ChatType.PRIVATE & filters.User(user_id=allowed_user_ids)


def collect_status() -> str:
    """現在の状態を文章にする。時間のかかる処理を含むのでスレッドで呼ぶ。"""

    lines = []

    for target in system.LABELS:
        state = system.service_state(target)
        heartbeat = system.read_heartbeat(target)

        if state["running"]:
            label = f"稼働中 pid={state['pid']}"
        elif system.stop_marker(target).exists():
            label = "停止中（指示による）"
        else:
            label = "停止" if state["loaded"] else "未登録"

        detail = (
            f"{heartbeat.get('commit', '?')}、応答 {system.age_text(heartbeat['time'])}"
            if heartbeat else "記録なし"
        )
        lines.append(f"{target}: {label}（{detail}）")

    memory = system.memory_free_percent()
    lines.append(f"メモリ空き: {memory}%" if memory is not None else "メモリ空き: 取得できません")

    swap = system.swap_usage()
    if swap:
        lines.append(f"スワップ: {swap}")

    lines.append(f"ディスク空き: {system.disk_free_gb():.1f}GB")

    github = system.github_main_status()
    lines.append(
        f"GitHub main: {github['commit']}（CI: {github['ci']}）"
        if github else "GitHub main: 取得できません"
    )

    return "\n".join(lines)


def check_health() -> set[str]:
    """通知すべき問題の集合を返す。"""

    problems = set()

    if not system.stop_marker("prod").exists():
        state = system.service_state("prod")
        heartbeat = system.read_heartbeat("prod")

        if not state["running"]:
            problems.add("本番Botが停止しています")
        elif heartbeat is None or time.time() - heartbeat["time"] > HEARTBEAT_STALE_SECONDS:
            problems.add("本番Botの応答記録が更新されていません")

    memory = system.memory_free_percent()
    if memory is not None and memory < MEMORY_FREE_ALERT_PERCENT:
        problems.add(f"メモリの空きが{MEMORY_FREE_ALERT_PERCENT}%未満です")

    if system.disk_free_gb() < DISK_FREE_ALERT_GB:
        problems.add(f"ディスクの空きが{DISK_FREE_ALERT_GB}GB未満です")

    return problems


def alert_messages(previous: set[str], current: set[str]) -> list[str]:
    """変化があった時だけ通知する。"""

    messages = [f"⚠️ {problem}" for problem in sorted(current - previous)]
    messages += [f"✅ 解消しました: {problem}" for problem in sorted(previous - current)]
    return messages


async def notify_all(bot, text):
    for user_id in ALLOWED_TELEGRAM_USER_IDS:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            print("[Ops] 通知エラー:", type(e).__name__)


async def monitor_loop(bot):
    known_problems = set()
    commit = await asyncio.to_thread(current_commit)

    while True:
        try:
            write_heartbeat(system.state_dir(), "ops", commit)
            current = await asyncio.to_thread(check_health)

            for message in alert_messages(known_problems, current):
                await notify_all(bot, message)

            known_problems = current

            for target in system.LABELS:
                await asyncio.to_thread(system.rotate_log, system.log_path(target))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[Ops] 監視エラー:", type(e).__name__)

        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(collect_status))


async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.args[0] if len(context.args) == 1 else None

    if target not in system.LABELS:
        await update.message.reply_text("使い方: /logs prod または /logs ops")
        return

    text = await asyncio.to_thread(system.tail_log, target)
    await update.message.reply_text(text[-MAX_MESSAGE_LENGTH:] or "ログはありません。")


async def _request_approval(update: Update, action: tuple[str, str], description: str):
    approval_id = approvals.add(action, description, update.effective_user.id)
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 実行", callback_data=f"{APPROVE}:{approval_id}"),
        InlineKeyboardButton("🚫 中止", callback_data=f"{REJECT}:{approval_id}"),
    ]])
    await update.message.reply_text(
        f"{description}\n実行しますか？（{DEFAULT_TTL_SECONDS // 60}分で無効になります）",
        reply_markup=buttons,
    )


async def request_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _request_approval(update, ("stop", "prod"), "本番Botを停止します。")


async def request_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _request_approval(update, ("start", "prod"), "本番Botを再開します。")


async def request_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.args[0] if len(context.args) == 1 else None

    if target not in system.LABELS:
        await update.message.reply_text("使い方: /restart prod または /restart ops")
        return

    await _request_approval(update, ("restart", target), f"{target} を再起動します。")


def stop_prod():
    system.stop_marker("prod").touch()

    try:
        system.stop_service("prod")
    except Exception:
        if system.service_state("prod")["running"]:
            system.stop_marker("prod").unlink(missing_ok=True)
        raise


def start_prod():
    system.start_service("prod")
    system.stop_marker("prod").unlink(missing_ok=True)


async def run_action(message, action: tuple[str, str]):
    operation, target = action

    if operation == "stop":
        await asyncio.to_thread(stop_prod)
        await message.reply_text("🛑 本番Botを停止しました。")
    elif operation == "start":
        await asyncio.to_thread(start_prod)
        await message.reply_text("▶️ 本番Botを再開しました。/status で確認できます。")
    elif operation == "restart" and target == "ops":
        # 自分自身を再起動すると以降は返信できないので、先に伝える
        await message.reply_text("🔄 Ops Botを再起動します。起動後に通知します。")
        await asyncio.to_thread(system.restart_service, target)
    elif operation == "restart":
        await asyncio.to_thread(system.restart_service, target)
        await message.reply_text(f"🔄 {target} を再起動しました。/status で確認できます。")
    else:
        raise ValueError("未知の操作です")


async def _acknowledge(query):
    try:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print("[Ops] ボタン更新エラー:", type(e).__name__)


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    if user_id not in ALLOWED_TELEGRAM_USER_IDS:
        await query.answer()
        return

    decision, _, approval_id = query.data.partition(":")
    pending = approvals.take(approval_id, user_id)

    await _acknowledge(query)

    if pending is None:
        await query.message.reply_text("この確認はすでに処理済みか、期限切れです。実行していません。")
        return

    if decision != APPROVE:
        await query.message.reply_text("🚫 中止しました。実行していません。")
        return

    try:
        await run_action(query.message, pending.plan)
    except Exception as e:
        print("[Ops] 操作エラー:", type(e).__name__)
        await query.message.reply_text(f"⚠️ 失敗しました: {system.redact(str(e))[:MAX_MESSAGE_LENGTH]}")


async def _on_start(app):
    await notify_all(app.bot, "🟢 Ops Botが起動しました。/status で状態を確認できます。")
    app.bot_data["monitor"] = asyncio.create_task(monitor_loop(app.bot))


async def _on_stop(app):
    task = app.bot_data.get("monitor")
    if task:
        task.cancel()


def build_application():
    allowed = build_filter(ALLOWED_TELEGRAM_USER_IDS)
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_on_start)
        .post_shutdown(_on_stop)
        .build()
    )

    for name, handler in [
        ("start", show_help),
        ("help", show_help),
        ("status", show_status),
        ("logs", show_logs),
        ("stop_prod", request_stop),
        ("start_prod", request_start),
        ("restart", request_restart),
    ]:
        app.add_handler(CommandHandler(name, handler, filters=allowed))

    app.add_handler(CallbackQueryHandler(on_decision, pattern=f"^({APPROVE}|{REJECT}):"))

    return app


def main():
    if not ALLOWED_TELEGRAM_USER_IDS:
        raise SystemExit("ALLOWED_TELEGRAM_USER_IDS が未設定のため起動しません。")

    print("Ops Bot起動")
    build_application().run_polling()


if __name__ == "__main__":
    main()
