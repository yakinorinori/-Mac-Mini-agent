from agent.process import run_command


def excel_open(path):
    """指定したExcelファイルを開く"""

    run_command(["open", path])
