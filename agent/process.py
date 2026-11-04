"""外部コマンドを実行し、失敗を例外として呼び出し側へ伝える。"""
import subprocess

DEFAULT_TIMEOUT_SECONDS = 120
_MAX_ERROR_OUTPUT = 500


class CommandError(RuntimeError):
    """外部コマンドが起動できない、タイムアウトした、または失敗した。"""


def run_command(args, timeout=DEFAULT_TIMEOUT_SECONDS):
    """コマンドを実行する。引数の値はメッセージに含めない(本文などを漏らさないため)。"""

    name = args[0]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise CommandError(f"{name} が{timeout}秒以内に終了しませんでした") from e
    except OSError as e:
        raise CommandError(f"{name} を実行できませんでした: {e.strerror}") from e

    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:_MAX_ERROR_OUTPUT]
        raise CommandError(f"{name} が失敗しました(終了コード{result.returncode}): {detail}")

    return result


def run_applescript(script, *values, timeout=DEFAULT_TIMEOUT_SECONDS):
    """AppleScriptを実行する。値はソースへ埋め込まず、`on run argv` の引数として渡す。

    `--` は、"-" で始まる値を osascript のオプションとして解釈させないために付ける。
    """

    return run_command(["osascript", "-e", script, "--", *values], timeout=timeout)
