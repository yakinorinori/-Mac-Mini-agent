from agent.models import Tool
from .excel_open import excel_open


def register(registry):

    registry.register(
        Tool(
            name="excel_open",
            function=excel_open,
            description="""
指定したExcelファイルを開く。

引数:
path

例
/Users/user/Documents/data.xlsx
~/Desktop/report.xlsx
"""
        )
    )
