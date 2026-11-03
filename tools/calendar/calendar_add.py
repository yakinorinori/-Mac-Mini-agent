from datetime import datetime

from agent.process import run_applescript

_ADD_EVENT_SCRIPT = '''
on run argv
    set eventTitle to item 1 of argv
    set startDate to date (item 2 of argv)

    tell application "Calendar"
        tell (first calendar whose writable is true)
            make new event with properties {summary:eventTitle, start date:startDate}
        end tell
    end tell
end run
'''


def calendar_add(title: str, date: str, time: str = "09:00"):
    """カレンダーにイベントを追加する（Calendar.app経由）

    引数:
        title: イベントのタイトル
        date:  日付 (例: "2026/08/26")
        time:  時刻 (例: "14:00")
    """

    try:
        datetime.strptime(f"{date} {time}", "%Y/%m/%d %H:%M")
    except ValueError as e:
        raise ValueError("日時は YYYY/MM/DD と HH:MM の形式で指定してください") from e

    run_applescript(_ADD_EVENT_SCRIPT, title, f"{date} {time}:00")
