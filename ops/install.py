"""常駐サービスをlaunchdへ登録する。

使い方: python -m ops.install [--ref ブランチ名] [--dry-run | --uninstall]
"""
import argparse
import plistlib
import stat
import sys
from pathlib import Path

from agent.process import CommandError, run_command
from ops.deploy import Deployer, Paths, link_target
from ops.system import (
    LABELS,
    bootstrap_service,
    launchd_domain,
    plist_path,
    staging_plist_path,
    wait_until_unloaded,
)

PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
THROTTLE_SECONDS = 30
REQUIRED_ENV_FILES = ("prod", "ops")
AGENT_SERVICES = ("prod", "ops", "runner")


def render_plist(target: str, ops_repo: Path, root: Path) -> bytes:
    state = root / "state"
    log = str(state / "logs" / f"{target}.log")
    code = {
        "prod": root / "prod",
        "staging": root / "staging",
        "ops": ops_repo,
        "runner": ops_repo,
    }[target]
    arguments = {
        "prod": ["main.py"],
        "staging": ["main.py"],
        "ops": ["-m", "ops.bot"],
        "runner": ["-m", "ops.runner"],
    }[target]
    env_name = "ops" if target == "runner" else target
    environment = {
        "AGENT_ENV_FILE": str(state / f"{env_name}.env"),
        "AGENT_STATE_DIR": str(state),
        "AGENT_NAME": target,
        "PATH": PATH,
        "PYTHONUNBUFFERED": "1",
    }
    data = {
        "Label": LABELS[target],
        "ProgramArguments": [str(code / ".venv" / "bin" / "python"), *arguments],
        "WorkingDirectory": str(code),
        "EnvironmentVariables": environment,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }

    if target == "runner":
        data["QueueDirectories"] = [str(state / "requests")]
    elif target == "staging":
        data["RunAtLoad"] = True
    else:
        data.update(RunAtLoad=True, KeepAlive=True, ThrottleInterval=THROTTLE_SECONDS)

    return plistlib.dumps(data)


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

    wait_until_unloaded(target)


def _prepare_state(root: Path):
    state = root / "state"

    for directory in (state, state / "logs", state / "heartbeat", state / "requests"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)


def install(ops_repo: Path, root: Path, ref="main", dry_run=False) -> int:
    state = root / "state"
    errors = [error for name in REQUIRED_ENV_FILES for error in check_env_file(state / f"{name}.env")]

    if (state / "staging.env").exists():
        errors += check_env_file(state / "staging.env")

    if not (ops_repo / ".venv" / "bin" / "python").is_file():
        errors.append(f"{ops_repo / '.venv'} がありません")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    if dry_run:
        for target in (*AGENT_SERVICES, "staging"):
            print(render_plist(target, ops_repo, root).decode())
        return 0

    _prepare_state(root)
    paths = Paths(root)

    if link_target(paths.prod) is None:
        print("最初のリリースを準備しています。数分かかることがあります。")
        print(f"本番のコミット: {Deployer(paths).initialize(ref)[:7]}")

    staging_plist_path().write_bytes(render_plist("staging", ops_repo, root))

    for target in AGENT_SERVICES:
        path = plist_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        _bootout(target)
        path.write_bytes(render_plist(target, ops_repo, root))
        run_command(["plutil", "-lint", str(path)], timeout=15)
        bootstrap_service(path)
        print(f"登録しました: {LABELS[target]}")

    (state / "prod.stopped").unlink(missing_ok=True)
    return 0


def uninstall() -> int:
    _bootout("staging")
    staging_plist_path().unlink(missing_ok=True)

    for target in AGENT_SERVICES:
        _bootout(target)
        plist_path(target).unlink(missing_ok=True)
        print(f"解除しました: {LABELS[target]}")

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--ref", default="main", help="最初に本番へ置くブランチ")
    parser.add_argument("--root", type=Path, default=Path.home() / "agent")
    args = parser.parse_args(argv)

    if args.uninstall:
        return uninstall()

    return install(
        Path(__file__).resolve().parent.parent, args.root, ref=args.ref, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
