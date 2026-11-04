"""Claude Code Remote ControlをMac miniで常駐させる。

手順:
  python -m ops.install_dev --prepare   作業場所と権限設定を用意する
  （~/agent/dev で claude remote-control を一度実行し、確認に答える）
  python -m ops.install_dev             launchdへ登録する
  python -m ops.install_dev --uninstall 登録を解除する
"""
import argparse
import json
import os
import plistlib
import shutil
import sys
from pathlib import Path

from agent.process import CommandError, run_command
from ops.deploy import REMOTE_URL
from ops.system import LABELS, bootstrap_service, launchd_domain, plist_path, wait_until_unloaded

TARGET = "remote-control"
SERVER_ARGUMENTS = [
    "remote-control",
    "--spawn", "worktree",
    "--capacity", "2",
    "--permission-mode", "default",
]
THROTTLE_SECONDS = 30
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# 本番の秘密情報と本番コードをClaude Codeから守る設定
SETTINGS = {
    "permissions": {
        "deny": [
            "Read(~/agent/state/**)",
            "Edit(~/agent/state/**)",
            "Edit(~/agent/prod/**)",
            "Edit(~/agent/releases/**)",
            "Edit(~/TelegramBot/**)",
            "Read(~/TelegramBot/.env)",
            "Read(~/.ssh/**)",
            "Read(~/.aws/**)",
            "Bash(launchctl *)",
        ],
    },
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        # 認証情報が必要な操作だけ外で実行する。実行前の承認は残る
        "excludedCommands": ["git push *", "gh *"],
        "filesystem": {
            "denyRead": ["~/agent/state", "~/.ssh", "~/.aws"],
        },
        "network": {
            "allowedDomains": [
                "github.com",
                "api.github.com",
                "pypi.org",
                "files.pythonhosted.org",
            ],
        },
    },
}


def merge_settings(current, required):
    """既存の設定を残して追加する。リストは和集合、辞書は再帰的に統合する。"""

    merged = dict(current)

    for key, value in required.items():
        existing = merged.get(key)

        if isinstance(value, dict):
            merged[key] = merge_settings(existing if isinstance(existing, dict) else {}, value)
        elif isinstance(value, list):
            base = existing if isinstance(existing, list) else []
            merged[key] = base + [item for item in value if item not in base]
        else:
            merged[key] = value

    return merged


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def apply_settings(path: Path):
    current = {}

    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(current, dict):
            raise ValueError(f"{path} の形式が想定と異なります")

        shutil.copy2(path, path.with_name(path.name + ".before-agent"))

    if any(name in current.get("env", {}) for name in FORBIDDEN_ENV):
        raise ValueError(f"{path} の env からAPIキーの設定を外してください")

    merged = merge_settings(current, SETTINGS)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ensure_clone(dev: Path, remote: str = REMOTE_URL):
    if (dev / ".git").exists():
        return False

    dev.parent.mkdir(parents=True, exist_ok=True)
    run_command(["git", "clone", remote, str(dev)], timeout=300)
    return True


def find_claude():
    found = shutil.which("claude", path=f"{Path.home() / '.local/bin'}:{BASE_PATH}:{os.environ.get('PATH', '')}")
    return Path(found) if found else None


def render_plist(claude: Path, dev: Path, state: Path) -> bytes:
    log = str(state / "logs" / f"{TARGET}.log")

    return plistlib.dumps({
        "Label": LABELS[TARGET],
        "ProgramArguments": [str(claude), *SERVER_ARGUMENTS],
        "WorkingDirectory": str(dev),
        # 環境は引き継がない。APIキーがあるとRemote Controlが動かないため
        "EnvironmentVariables": {"PATH": f"{claude.parent}:{BASE_PATH}"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def prepare(root: Path) -> int:
    claude = find_claude()

    if claude is None:
        print("claude コマンドが見つかりません。Claude Codeをインストールしてください。", file=sys.stderr)
        return 1

    try:
        apply_settings(settings_path())
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    dev = root / "dev"
    ensure_clone(dev)
    print(f"用意しました: {dev}")
    print("次に確認を済ませてください:")
    print(f"  cd {dev}")
    print(f"  claude {' '.join(SERVER_ARGUMENTS)}")
    return 0


def register(root: Path) -> int:
    claude = find_claude()
    dev = root / "dev"

    if claude is None or not (dev / ".git").exists():
        print("先に python -m ops.install_dev --prepare を実行してください。", file=sys.stderr)
        return 1

    if not settings_path().exists() or SETTINGS["sandbox"]["enabled"] is not json.loads(
        settings_path().read_text(encoding="utf-8")
    ).get("sandbox", {}).get("enabled"):
        print("権限設定がありません。--prepare を実行してください。", file=sys.stderr)
        return 1

    state = root / "state"
    (state / "logs").mkdir(parents=True, exist_ok=True)
    _bootout()
    path = plist_path(TARGET)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_plist(claude, dev, state))
    run_command(["plutil", "-lint", str(path)], timeout=15)
    bootstrap_service(path)
    print(f"登録しました: {LABELS[TARGET]}")
    return 0


def _bootout():
    try:
        run_command(["launchctl", "bootout", f"{launchd_domain()}/{LABELS[TARGET]}"], timeout=60)
    except CommandError:
        pass

    wait_until_unloaded(TARGET)


def uninstall() -> int:
    _bootout()
    plist_path(TARGET).unlink(missing_ok=True)
    print(f"解除しました: {LABELS[TARGET]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--root", type=Path, default=Path.home() / "agent")
    args = parser.parse_args(argv)

    if args.uninstall:
        return uninstall()

    return prepare(args.root) if args.prepare else register(args.root)


if __name__ == "__main__":
    sys.exit(main())
