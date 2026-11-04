from pydantic import Field

from agent.models import Tool
from agent.schemas import SingleLine, ToolArgs
from .calendar_add import calendar_add


class CalendarAddArgs(ToolArgs):
    title: SingleLine = Field(min_length=1, max_length=200)
    date: str = Field(pattern=r"^\d{4}/\d{1,2}/\d{1,2}$")
    time: str = Field(default="09:00", pattern=r"^\d{1,2}:\d{2}$")


def register(registry):

    registry.register(
        Tool(
            name="calendar_add",
            args_schema=CalendarAddArgs,
            function=calendar_add,
            description="""
カレンダーにイベントを追加する。終了時刻は開始の1時間後になる。

引数:
title : イベントのタイトル
date  : 日付 (例: 2026/08/26)
time  : 時刻 (例: 14:00) ※省略時は 09:00

例
title="会議", date="2026/08/26", time="14:00"
"""
        )
    )
