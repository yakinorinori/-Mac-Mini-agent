from pydantic import Field

from agent.models import Tool
from agent.schemas import ToolArgs
from .excel_open import excel_open


class ExcelOpenArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=1024)


def register(registry):

    registry.register(
        Tool(
            name="excel_open",
            args_schema=ExcelOpenArgs,
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
