"""運用専用のTelegram Bot。LLMやツールを読み込まず、固定のコマンドだけを受け付ける。"""
import asyncio
import json
import os
import re
import secrets
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from ops import github, system
from ops.deploy import default_paths, is_valid_branch, link_target

MONITOR_INTERVAL_SECONDS = 60
# 今の本番Botは計画中に処理が止まることがあるので、長めに判定する
HEARTBEAT_STALE_SECONDS = 300
# これより長く残る反映の記録は、中断の可能性があるとして通知する
DEPLOY_STUCK_SECONDS = 20 * 60
MEMORY_FREE_ALERT_PERCENT = 10
DISK_FREE_ALERT_GB = 5
MAX_MESSAGE_LENGTH = 3500

RESTART_TARGETS = ("prod", "ops", "remote-control")

PR_NOTICES_FILE = "pr-notified.json"
PR_NOTICES_LIMIT = 200
_PR_ACTION = re.compile(r"^(pr_staging|pr_deploy|pr_close):([1-9][0-9]{0,8}):([0-9a-f]{12})$")
_ROLLBACK_ACTION = re.compile(r"^rollback_to:([0-9a-f]{12})$")
_CI_PENDING = {"未実行", "queued", "in_progress", "waiting", "requested", "pending"}

COMMANDS = [
    ("prs", "開いているPRと操作ボタン"),
    ("status", "稼働状況"),
    ("deploy", "mainを本番へ反映"),
    ("rollback", "1つ前へ戻す"),
    ("stop_staging", "検証Botを停止"),
    ("logs", "ログ（例: /logs prod）"),
    ("help", "使い方"),
]

APPROVE = "ops_approve"
REJECT = "ops_reject"

HELP = """運用コマンド:
/prs 開いているPRと操作ボタン
/status 各プロセス、コミット、メモリ、ディスク、CI
/logs prod|staging|ops|runner|remote-control 直近のログ
/deploy mainの最新を本番へ反映（承認あり）
/rollback 直前のリリースへ戻す（承認あり）
/deploy_staging ブランチ名 stagingへ反映（承認あり）
/stop_staging stagingを停止
/stop_prod 本番Botを停止（承認あり）
/start_prod 本番Botを再開（承認あり）
/restart prod|ops|remote-control 再起動（承認あり）"""

approvals = ApprovalStore()


def build_filter(allowed_user_ids):
    return filters.ChatType.PRIVATE & filters.User(user_id=allowed_user_ids)


def enqueue(action: str, **fields):
    """runnerへの要求を置く。書きかけを読まれないよう、完成してから移動する。"""

    requests = system.state_dir() / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{secrets.token_hex(4)}.json"
    temporary = system.state_dir() / f".{name}.tmp"
    temporary.write_text(json.dumps({"action": action, **fields}), encoding="utf-8")
    os.replace(temporary, requests / name)


def collect_status() -> str:
    """現在の状態を文章にする。時間のかかる処理を含むのでスレッドで呼ぶ。"""

    lines = []

    for target in ("prod", "staging", "ops", "remote-control"):
        state = system.service_state(target)
        heartbeat = system.read_heartbeat(target)

        if state["running"]:
            label = f"稼働中 pid={state['pid']}"
        elif system.stop_marker(target).exists():
            label = "停止中（指示による）"
        else:
            label = "停止" if state["loaded"] else "未登録"

        if target == "remote-control":
            lines.append(f"{target}: {label}")
            continue

        detail = (
            f"{heartbeat.get('commit', '?')}、応答 {system.age_text(heartbeat['time'])}"
            if heartbeat else "記録なし"
        )
        lines.append(f"{target}: {label}（{detail}）")

    paths = default_paths()
    previous = link_target(paths.previous)
    lines.append(f"戻せるリリース: {previous[:7] if previous else 'なし'}")

    if paths.journal.exists():
        lines.append("反映を処理中です")

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
    journal = default_paths().journal

    try:
        deploying = True
        if time.time() - journal.stat().st_mtime > DEPLOY_STUCK_SECONDS:
            problems.add("反映の処理が終わっていない可能性があります")
    except OSError:
        deploying = False

    if not deploying and not system.stop_marker("prod").exists():
        state = system.service_state("prod")
        heartbeat = system.read_heartbeat("prod")

        if not state["running"]:
            problems.add("本番Botが停止しています")
        elif heartbeat is None or time.time() - heartbeat["time"] > HEARTBEAT_STALE_SECONDS:
            problems.add("本番Botの応答記録が更新されていません")

    if system.plist_path("remote-control").exists() and not system.service_state("remote-control")["running"]:
        problems.add("Remote Controlが停止しています")

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


def pr_buttons(pull: dict) -> InlineKeyboardMarkup:
    key = f"{pull['number']}:{pull['sha'][:12]}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 検証する", callback_data=f"pr_staging:{key}")],
        [InlineKeyboardButton("🚀 マージして本番へ", callback_data=f"pr_deploy:{key}")],
        [InlineKeyboardButton("🚫 閉じる", callback_data=f"pr_close:{key}")],
    ])


def pr_text(pull: dict, conclusion) -> str:
    state = {"success": "CI成功"}.get(conclusion, f"CI: {conclusion}")
    return (
        f"PR #{pull['number']} {pull['title']}\n"
        f"{pull['branch']}（{pull['sha'][:7]}）\n{state}\n{pull['url']}"
    )


def _load_pr_notices() -> list:
    try:
        data = json.loads((system.state_dir() / PR_NOTICES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    return data if isinstance(data, list) else []


def _save_pr_notices(notices: list):
    temporary = system.state_dir() / f".{PR_NOTICES_FILE}.tmp"
    temporary.write_text(json.dumps(notices[-PR_NOTICES_LIMIT:]), encoding="utf-8")
    os.replace(temporary, system.state_dir() / PR_NOTICES_FILE)


def collect_pr_notices() -> list[tuple]:
    """CIが終わった未通知のPRを返す。同じコミットは再起動後も重ねて通知しない。"""

    notices = _load_pr_notices()
    results = []

    for pull in github.open_pull_requests():
        key = f"{pull['number']}:{pull['sha']}"

        if not pull["same_repo"] or pull["draft"] or key in notices:
            continue

        conclusion = github.ci_conclusion(pull["sha"])

        if conclusion in _CI_PENDING or conclusion is None:
            continue

        notices.append(key)
        results.append((pull, conclusion))

    if results:
        _save_pr_notices(notices)

    return results


async def send_pr_notices(bot):
    for pull, conclusion in await asyncio.to_thread(collect_pr_notices):
        markup = pr_buttons(pull) if conclusion == "success" else None
        await notify_all(bot, pr_text(pull, conclusion), reply_markup=markup)


async def notify_all(bot, text, reply_markup=None):
    for user_id in ALLOWED_TELEGRAM_USER_IDS:
        try:
            await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
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

            if github.has_token():
                await send_pr_notices(bot)

            for target in system.LABELS:
                await asyncio.to_thread(system.rotate_log, system.log_path(target))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[Ops] 監視エラー:", type(e).__name__)

        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def show_prs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not github.has_token():
        await update.message.reply_text("GITHUB_TOKEN が未設定です。Mac miniの ops.env に設定してください。")
        return

    try:
        pulls = await asyncio.to_thread(github.open_pull_requests)
        pulls = [pull for pull in pulls if pull["same_repo"] and not pull["draft"]]

        if not pulls:
            await update.message.reply_text("開いているPRはありません。")
            return

        for pull in pulls:
            conclusion = await asyncio.to_thread(github.ci_conclusion, pull["sha"])
            markup = pr_buttons(pull) if conclusion == "success" else None
            await update.message.reply_text(pr_text(pull, conclusion), reply_markup=markup)
    except github.GitHubError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(collect_status))


async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.args[0] if len(context.args) == 1 else None

    if target not in system.LABELS:
        await update.message.reply_text("使い方: /logs prod|staging|ops|runner|remote-control")
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

    if target not in RESTART_TARGETS:
        await update.message.reply_text("使い方: /restart prod|ops|remote-control")
        return

    await _request_approval(update, ("restart", target), f"{target} を再起動します。")


async def request_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    github = await asyncio.to_thread(system.github_main_status)

    if github is None:
        await update.message.reply_text("GitHubからmainの状態を取得できません。")
        return

    if github["ci"] != "success":
        await update.message.reply_text(
            f"main（{github['commit']}）のCIが成功していないので反映できません（{github['ci']}）。"
        )
        return

    current = link_target(default_paths().prod)

    if current == github["sha"]:
        await update.message.reply_text(f"{github['commit']} は反映済みです。")
        return

    await _request_approval(
        update,
        ("deploy", github["sha"]),
        f"本番を {current[:7] if current else 'なし'} から {github['commit']} へ更新します。",
    )


async def request_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paths = default_paths()
    previous = link_target(paths.previous)

    if not previous:
        await update.message.reply_text("戻せるリリースがありません。")
        return

    current = link_target(paths.prod)
    await _request_approval(
        update,
        ("rollback", previous),
        f"本番を {current[:7] if current else 'なし'} から {previous[:7]} へ戻します。",
    )


async def request_deploy_staging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = context.args[0] if len(context.args) == 1 else None

    if not is_valid_branch(branch):
        await update.message.reply_text("使い方: /deploy_staging ブランチ名")
        return

    if not (system.state_dir() / "staging.env").is_file():
        await update.message.reply_text("staging.env がありません。先にMac miniで作成してください。")
        return

    await _request_approval(
        update,
        ("deploy_staging", branch),
        f"stagingへ {branch} を反映します。stagingはこのMac上で実際に操作を実行できます。",
    )


async def stop_staging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enqueue("stop_staging")
    await update.message.reply_text("stagingの停止を受け付けました。")


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
    elif operation == "deploy":
        enqueue("deploy", sha=target)
        await message.reply_text("反映を受け付けました。数分かかることがあります。結果は通知します。")
    elif operation == "rollback":
        enqueue("rollback")
        await message.reply_text("切り戻しを受け付けました。結果は通知します。")
    elif operation == "deploy_staging":
        enqueue("deploy_staging", branch=target)
        await message.reply_text("stagingへの反映を受け付けました。結果は通知します。")
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


async def _clear_buttons(query):
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        print("[Ops] ボタン更新エラー:", type(e).__name__)


async def on_pr_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRのボタンを処理する。押した時点でPRの状態を確かめ直す。"""

    query = update.callback_query

    if update.effective_user.id not in ALLOWED_TELEGRAM_USER_IDS:
        await query.answer()
        return

    await query.answer()
    match = _PR_ACTION.match(query.data)

    if match is None:
        await query.message.reply_text("このボタンは無効です。")
        return

    action, number, short_sha = match.group(1), int(match.group(2)), match.group(3)

    try:
        pull = await asyncio.to_thread(github.get_pull_request, number)
    except github.GitHubError as e:
        await query.message.reply_text(f"⚠️ {e}")
        return

    if pull["state"] != "open" or not pull["sha"].startswith(short_sha) or not pull["same_repo"]:
        await _clear_buttons(query)
        await query.message.reply_text(f"PR #{number} は変更されたか閉じられています。/prs で確認してください。")
        return

    if action == "pr_staging":
        if not (system.state_dir() / "staging.env").is_file():
            await query.message.reply_text("staging.env がありません。先にMac miniで作成してください。")
            return

        enqueue("deploy_staging", branch=pull["branch"])
        await query.message.reply_text(f"🧪 {pull['branch']} をstagingへ反映します。結果は通知します。")
    elif action == "pr_deploy":
        await _clear_buttons(query)
        enqueue("merge_deploy", number=number, sha=pull["sha"])
        await query.message.reply_text(f"🚀 PR #{number} をマージして本番へ反映します。結果は通知します。")
    else:
        await _clear_buttons(query)

        try:
            await asyncio.to_thread(github.close_pull_request, number)
            await query.message.reply_text(f"🚫 PR #{number} を閉じました。")
        except github.GitHubError as e:
            await query.message.reply_text(f"⚠️ {e}")


async def on_rollback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """通知の [戻す] を処理する。その後に本番が変わっていたら戻さない。"""

    query = update.callback_query

    if update.effective_user.id not in ALLOWED_TELEGRAM_USER_IDS:
        await query.answer()
        return

    await query.answer()
    await _clear_buttons(query)
    match = _ROLLBACK_ACTION.match(query.data)
    current = link_target(default_paths().prod)

    if match is None or not current or not current.startswith(match.group(1)):
        await query.message.reply_text("本番がその後に変わったので戻しません。/status で確認してください。")
        return

    enqueue("rollback")
    await query.message.reply_text("↩️ 1つ前へ戻します。結果は通知します。")


async def _set_command_menu(bot):
    try:
        await bot.set_my_commands([BotCommand(name, text) for name, text in COMMANDS])
    except Exception as e:
        print("[Ops] メニュー設定エラー:", type(e).__name__)


async def _on_start(app):
    await _set_command_menu(app.bot)
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
        ("deploy", request_deploy),
        ("rollback", request_rollback),
        ("deploy_staging", request_deploy_staging),
        ("stop_staging", stop_staging),
        ("prs", show_prs),
    ]:
        app.add_handler(CommandHandler(name, handler, filters=allowed))

    app.add_handler(CallbackQueryHandler(on_decision, pattern=f"^({APPROVE}|{REJECT}):"))
    app.add_handler(CallbackQueryHandler(on_pr_action, pattern="^pr_"))
    app.add_handler(CallbackQueryHandler(on_rollback_button, pattern="^rollback_to:"))

    return app


def main():
    if not ALLOWED_TELEGRAM_USER_IDS:
        raise SystemExit("ALLOWED_TELEGRAM_USER_IDS が未設定のため起動しません。")

    print("Ops Bot起動")
    build_application().run_polling()


if __name__ == "__main__":
    main()
