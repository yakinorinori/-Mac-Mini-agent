"""launchdから起動され、Ops Botの要求を順に処理して終了する。"""
import fcntl
import json
import sys

import httpx

from config import ALLOWED_TELEGRAM_USER_IDS, TOKEN
from ops import github, system
from ops.deploy import (
    Deployer,
    DeployError,
    default_paths,
    is_valid_branch,
    is_valid_sha,
    link_target,
    short,
)

MAX_MESSAGE_LENGTH = 3500
MERGE_CI_TIMEOUT_SECONDS = 15 * 60
MERGE_CI_POLL_SECONDS = 15
_CI_PENDING = {None, "未実行", "queued", "in_progress", "waiting", "requested", "pending"}


def telegram_send(text: str, buttons=None):
    """URLにトークンを含むので、失敗時は例外の種類だけを記録する。"""

    print(system.redact(text))
    data = {"text": system.redact(text)[:MAX_MESSAGE_LENGTH]}

    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})

    for user_id in ALLOWED_TELEGRAM_USER_IDS:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": user_id, **data},
                timeout=15,
            ).raise_for_status()
        except httpx.HTTPError as e:
            print("[Runner] 通知エラー:", type(e).__name__)


def telegram_notify(text: str):
    telegram_send(text)


def offer_rollback(sha: str):
    telegram_send(
        f"問題があれば、{short(sha)} から1つ前へ戻せます。",
        [[{"text": "↩️ 戻す", "callback_data": f"rollback_to:{sha[:12]}"}]],
    )


def wait_for_ci(deployer: Deployer, sha: str):
    """マージ後のmainのCIが終わるまで待つ。"""

    deadline = deployer.clock() + MERGE_CI_TIMEOUT_SECONDS

    while deployer.clock() < deadline:
        conclusion = deployer.ci_conclusion(sha)

        if conclusion == "success":
            return

        if conclusion not in _CI_PENDING:
            raise DeployError(f"{short(sha)} のCIが失敗しました（{conclusion}）。本番は変更していません。")

        deployer.sleep(MERGE_CI_POLL_SECONDS)

    raise DeployError(f"{short(sha)} のCIが時間内に終わりませんでした。本番は変更していません。後で /deploy できます。")


def merge_and_deploy(number: int, sha: str, deployer: Deployer, api=github):
    """表示したコミットのままのPRだけをマージして本番へ反映する。"""

    pull = api.get_pull_request(number)

    if pull["state"] != "open" or pull["sha"] != sha or not pull["same_repo"]:
        raise DeployError(f"PR #{number} は通知の後に変更されたか、閉じられています。/prs で確認してください。")

    conclusion = api.ci_conclusion(sha)

    if conclusion != "success":
        raise DeployError(f"PR #{number} のCIが成功していません（{conclusion}）。マージしていません。")

    merged = api.merge_pull_request(number, sha)
    deployer.notify(f"🔀 PR #{number} をマージしました（{short(merged)}）。CIを待っています。")
    wait_for_ci(deployer, merged)
    deployer.deploy(merged)

    if link_target(deployer.paths.staging):
        deployer.stop_staging()

    return merged


def execute(request, deployer: Deployer, api=github, offer=offer_rollback):
    """要求の形式を検証してから実行する。"""

    action = request.get("action") if isinstance(request, dict) else None

    if action == "deploy" and is_valid_sha(request.get("sha")):
        deployer.deploy(request["sha"])
        deployed = request["sha"]
    elif action == "merge_deploy" and _valid_number(request.get("number")) and is_valid_sha(request.get("sha")):
        deployed = merge_and_deploy(request["number"], request["sha"], deployer, api)
    elif action == "rollback":
        deployer.rollback()
        return
    elif action == "deploy_staging" and is_valid_branch(request.get("branch")):
        deployer.deploy_staging(request["branch"])
        return
    elif action == "stop_staging":
        deployer.stop_staging()
        return
    else:
        raise DeployError("不正な要求を破棄しました")

    if link_target(deployer.paths.prod) == deployed:
        offer(deployed)


def _valid_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def process_requests(deployer: Deployer):
    deployer.recover()

    while True:
        pending = sorted(p for p in deployer.paths.requests.glob("*.json") if p.is_file())

        if not pending:
            return

        path = pending[0]

        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            request = None

        # 途中で止まっても同じ反映を繰り返さないよう、先に削除する
        path.unlink(missing_ok=True)

        try:
            execute(request, deployer)
        except (DeployError, github.GitHubError) as e:
            deployer.notify(f"⚠️ {e}")
        except Exception as e:
            print("[Runner] 想定外のエラー:", type(e).__name__)
            deployer.notify(f"🚨 運用処理でエラーが発生しました: {type(e).__name__}。/logs runner を確認してください。")


def main() -> int:
    paths = default_paths()
    paths.requests.mkdir(parents=True, exist_ok=True)

    with paths.lock.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0

        process_requests(Deployer(paths, notify=telegram_notify))

    return 0


if __name__ == "__main__":
    sys.exit(main())
