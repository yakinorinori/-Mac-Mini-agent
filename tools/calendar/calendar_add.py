import subprocess


def calendar_add(title: str, date: str, time: str = "09:00"):
    """カレンダーにイベントを追加する（Calendar.app経由）

    引数:
        title: イベントのタイトル
        date:  日付 (例: "2026/08/26")
        time:  時刻 (例: "14:00")
    """

    script = f'''
    tell application "Calendar"
        tell (first calendar whose writable is true)
            make new event with properties {{summary:"{title}", start date:(date "{date} {time}:00")}}
        end tell
    end tell
    '''

    subprocess.run(["osascript", "-e", script])
