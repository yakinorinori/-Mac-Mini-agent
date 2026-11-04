from pathlib import Path

from agent.process import run_command

_ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv"}


def excel_open(path):
    """指定したExcelファイルを開く"""

    target = Path(path).expanduser().resolve()

    if target.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError("開けるのはExcelまたはCSVのファイルだけです")

    if not target.is_file():
        raise FileNotFoundError(f"ファイルが見つかりません: {target}")

    run_command(["open", str(target)])
