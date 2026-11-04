"""launchdから起動され、Ops Botの要求を順に処理して終了する。"""
import fcntl
import json
import sys

import httpx

from config import ALLOWED_TELEGRAM_USER_IDS, TOKEN
from ops import system
from ops.deploy import Deployer, DeployError, default_paths, is_valid_branch, is_valid_sha

MAX_MESSAGE_LENGTH = 3500


def telegram_notify(text: str):
    """URLにトークンを含むので、失敗時は例外の種類だけを記録する。"""

    print(system.redact(text))

    for user_id in ALLOWED_TELEGRAM_USER_IDS:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": user_id, "text": system.redact(text)[:MAX_MESSAGE_LENGTH]},
                timeout=15,
            ).raise_for_status()
        except httpx.HTTPError as e:
            print("[Runner] 通知エラー:", type(e).__name__)


def execute(request, deployer: Deployer):
    """要求の形式を検証してから実行する。"""

    action = request.get("action") if isinstance(request, dict) else None

    if action == "deploy" and is_valid_sha(request.get("sha")):
        deployer.deploy(request["sha"])
    elif action == "rollback":
        deployer.rollback()
    elif action == "deploy_staging" and is_valid_branch(request.get("branch")):
        deployer.deploy_staging(request["branch"])
    elif action == "stop_staging":
        deployer.stop_staging()
    else:
        raise DeployError("不正な要求を破棄しました")


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
        except DeployError as e:
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
