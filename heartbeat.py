"""Botの生存記録を AGENT_STATE_DIR/heartbeat/ へ書き込む。"""
import json
import os
import subprocess
import time
from pathlib import Path

INTERVAL_SECONDS = 30


def state_dir():
    """常駐環境の状態フォルダを返す。手動起動などで未設定なら None を返す。"""

    value = os.getenv("AGENT_STATE_DIR")
    return Path(value) if value else None


def current_commit():
    root = Path(__file__).resolve().parent

    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True, cwd=root,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_heartbeat(directory, name, commit, now=None):
    """途中で読まれても壊れないよう、一時ファイル経由で置き換える。"""

    target_dir = Path(directory) / "heartbeat"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.json"
    temporary = target_dir / f".{name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps({
            "name": name,
            "commit": commit,
            "pid": os.getpid(),
            "time": time.time() if now is None else now,
        }),
        encoding="utf-8",
    )
    os.replace(temporary, target)
