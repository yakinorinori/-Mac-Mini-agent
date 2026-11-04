from agent.models import Tool
from .calendar_add import calendar_add


def register(registry):

    registry.register(
        Tool(
            name="calendar_add",
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
