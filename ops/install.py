"""本番BotとOps Botをlaunchdへ登録する。

使い方: python -m ops.install [--dry-run | --uninstall]
"""
import argparse
import plistlib
import stat
import sys
from pathlib import Path

from agent.process import CommandError, run_command
from ops.system import LABELS, launchd_domain, plist_path

PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
THROTTLE_SECONDS = 30
_ARGUMENTS = {"prod": ["main.py"], "ops": ["-m", "ops.bot"]}


def render_plist(target: str, repo: Path, state: Path) -> bytes:
    log = str(state / "logs" / f"{target}.log")

    return plistlib.dumps({
        "Label": LABELS[target],
        "ProgramArguments": [str(repo / ".venv" / "bin" / "python"), *_ARGUMENTS[target]],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "AGENT_ENV_FILE": str(state / f"{target}.env"),
            "AGENT_STATE_DIR": str(state),
            "PATH": PATH,
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def check_env_file(path: Path) -> list[str]:
    if not path.is_file():
        return [f"{path} がありません"]

    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        return [f"{path} の権限が広すぎます。chmod 600 を実行してください"]

    return []


def _bootout(target: str):
    try:
        run_command(["launchctl", "bootout", f"{launchd_domain()}/{LABELS[target]}"], timeout=60)
    except CommandError:
        pass


def install(repo: Path, state: Path, dry_run=False) -> int:
    errors = [error for target in LABELS for error in check_env_file(state / f"{target}.env")]

    if not (repo / ".venv" / "bin" / "python").is_file():
        errors.append(f"{repo / '.venv'} がありません")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    for target in LABELS:
        content = render_plist(target, repo, state)

        if dry_run:
            print(content.decode())
            continue

        (state / "logs").mkdir(parents=True, exist_ok=True)
        (state / "heartbeat").mkdir(parents=True, exist_ok=True)
        (state / f"{target}.stopped").unlink(missing_ok=True)
        path = plist_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        _bootout(target)
        path.write_bytes(content)
        run_command(["plutil", "-lint", str(path)], timeout=15)
        run_command(["launchctl", "bootstrap", launchd_domain(), str(path)], timeout=60)
        print(f"登録しました: {LABELS[target]}")

    return 0


def uninstall() -> int:
    for target in LABELS:
        _bootout(target)
        plist_path(target).unlink(missing_ok=True)
        print(f"解除しました: {LABELS[target]}")

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--state", type=Path, default=Path.home() / "agent" / "state")
    args = parser.parse_args(argv)

    if args.uninstall:
        return uninstall()

    return install(Path(__file__).resolve().parent.parent, args.state, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
