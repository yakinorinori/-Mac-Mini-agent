"""launchd、ログ、システム状態の操作。シェルを介さず引数のリストで実行する。"""
import json
import os
import re
import shutil
import time
from pathlib import Path

import httpx

from agent.process import CommandError, run_command

LABELS = {"prod": "com.agent.prod", "ops": "com.agent.ops"}
GITHUB_REPOSITORY = "yakinorinori/-Mac-Mini-agent"
LOG_MAX_BYTES = 5 * 1024 * 1024
TAIL_READ_BYTES = 64 * 1024

_SECRET_PATTERNS = [
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "[TELEGRAM_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[API_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[GITHUB_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (
        re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*)\s*[=:]\s*\S+"),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
]


def state_dir() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIR") or Path.home() / "agent" / "state")


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def plist_path(target: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABELS[target]}.plist"


def _service(target: str) -> str:
    return f"{launchd_domain()}/{LABELS[target]}"


def service_state(target: str) -> dict:
    try:
        output = run_command(["launchctl", "print", _service(target)], timeout=15).stdout
    except CommandError:
        return {"loaded": False, "running": False, "pid": None}

    state = re.search(r"^\s*state = (\S+)", output, re.MULTILINE)
    pid = re.search(r"^\s*pid = (\d+)", output, re.MULTILINE)

    return {
        "loaded": True,
        "running": bool(state and state.group(1) == "running"),
        "pid": int(pid.group(1)) if pid else None,
    }


def stop_service(target: str):
    run_command(["launchctl", "bootout", _service(target)], timeout=60)


def start_service(target: str):
    run_command(["launchctl", "bootstrap", launchd_domain(), str(plist_path(target))], timeout=60)


def restart_service(target: str):
    run_command(["launchctl", "kickstart", "-k", _service(target)], timeout=60)


def stop_marker(target: str) -> Path:
    return state_dir() / f"{target}.stopped"


def read_heartbeat(name: str):
    try:
        data = json.loads((state_dir() / "heartbeat" / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) and isinstance(data.get("time"), (int, float)) else None


def memory_free_percent():
    try:
        output = run_command(["memory_pressure"], timeout=15).stdout
    except CommandError:
        return None

    match = re.search(r"free percentage:\s*(\d+)%", output)
    return int(match.group(1)) if match else None


def swap_usage():
    try:
        return run_command(["sysctl", "-n", "vm.swapusage"], timeout=15).stdout.strip() or None
    except CommandError:
        return None


def disk_free_gb() -> float:
    return shutil.disk_usage(state_dir()).free / 1024 ** 3


def github_main_status():
    """mainの最新コミットとCI結果を返す。取得できなければ None を返す。"""

    base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
    headers = {"Accept": "application/vnd.github+json"}

    try:
        with httpx.Client(timeout=8, headers=headers) as client:
            commit = client.get(f"{base}/commits/main")
            commit.raise_for_status()
            sha = commit.json()["sha"]
            checks = client.get(f"{base}/commits/{sha}/check-runs")
            checks.raise_for_status()
    except (httpx.HTTPError, KeyError, ValueError):
        return None

    runs = [run for run in checks.json().get("check_runs", []) if run.get("name") == "test"]
    result = (runs[0].get("conclusion") or runs[0].get("status")) if runs else "未実行"

    return {"commit": sha[:7], "ci": result}


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)

    return text


def log_path(target: str) -> Path:
    return state_dir() / "logs" / f"{target}.log"


def tail_log(target: str, lines: int = 50) -> str:
    """ログの末尾を、秘密情報を伏せて返す。"""

    try:
        with log_path(target).open("rb") as file:
            file.seek(0, os.SEEK_END)
            file.seek(max(0, file.tell() - TAIL_READ_BYTES))
            content = file.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    return redact("\n".join(content.splitlines()[-lines:]))


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES):
    """launchdが開いたままのファイルも扱えるよう、コピーしてから中身を切り詰める。"""

    try:
        if path.stat().st_size <= max_bytes:
            return
        shutil.copyfile(path, path.with_name(path.name + ".1"))
        os.truncate(path, 0)
    except OSError as e:
        print(f"[Ops] ログのローテーション失敗: {path.name}: {e.strerror}")


def age_text(timestamp) -> str:
    seconds = max(0, int(time.time() - timestamp))

    if seconds < 120:
        return f"{seconds}秒前"
    if seconds < 7200:
        return f"{seconds // 60}分前"
    return f"{seconds // 3600}時間前"
