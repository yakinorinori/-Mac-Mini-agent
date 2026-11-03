import subprocess


def excel_open(path):
    """指定したExcelファイルを開く"""

    subprocess.run(["open", path])
