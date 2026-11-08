"""よくある形のカレンダー追加依頼を、LLMを使わずに読み取る。

確実に読めるものだけを返す。時刻がない文や相対的な曜日などは None を返してLLMへ回す。
実行前の承認は通常どおり必要で、読み取った内容はそこで確認できる。
"""
import re
from datetime import date, datetime, timedelta

from agent.routing import is_simple

_DIGITS = str.maketrans("０１２３４５６７８９：／", "0123456789:/")

_DATE = (
    r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"|(?P<syear>\d{4})/(?P<smonth>\d{1,2})/(?P<sday>\d{1,2})"
    r"|(?P<relative>今日|本日|明日|あした|明後日|あさって)"
)
_TIME = (
    r"(?:(?P<period>午前|午後))?(?P<hour>\d{1,2})時(?:(?P<half>半)|(?P<minute>\d{1,2})分)?"
    r"|(?P<chour>\d{1,2}):(?P<cminute>\d{2})"
)
_WHEN = rf"(?:{_DATE})の?\s*(?:{_TIME})"
_VERB = r"(?:追加|入れ|登録)(?:して|て)?(?:ください|下さい|お願いします)?\s*[。!！]*"
_TITLE = r"(?:「(?P<title>[^「」\n]{1,100})」|(?P<plain>[^「」\n\s]{1,100}?))"
_PATTERNS = [
    re.compile(
        rf"^カレンダーに「(?P<title>[^「」\n]{{1,100}})」(?:という予定)?を\s*{_WHEN}(?:に|から)?\s*{_VERB}$"
    ),
    re.compile(rf"^カレンダーに\s*{_WHEN}(?:に|の|から)\s*{_TITLE}(?:という予定)?を\s*{_VERB}$"),
    re.compile(rf"^{_WHEN}(?:に|の|から)\s*{_TITLE}(?:という予定)?を\s*カレンダーに\s*{_VERB}$"),
]
_RELATIVE_DAYS = {"今日": 0, "本日": 0, "明日": 1, "あした": 1, "明後日": 2, "あさって": 2}


def _resolve_date(groups: dict, now: datetime):
    today = now.date()

    if groups.get("relative"):
        return today + timedelta(days=_RELATIVE_DAYS[groups["relative"]])

    if groups.get("syear"):
        return date(int(groups["syear"]), int(groups["smonth"]), int(groups["sday"]))

    month, day = int(groups["month"]), int(groups["day"])

    if groups.get("year"):
        return date(int(groups["year"]), month, day)

    # 年がなければ、次に来るその日付とする
    candidate = date(today.year, month, day)
    return candidate if candidate >= today else date(today.year + 1, month, day)


def _resolve_time(groups: dict):
    if groups.get("chour"):
        hour, minute = int(groups["chour"]), int(groups["cminute"])
    else:
        hour = int(groups["hour"])
        minute = 30 if groups.get("half") else int(groups.get("minute") or 0)
        period = groups.get("period")

        if period and not 1 <= hour <= 12:
            return None
        if period == "午後" and hour != 12:
            hour += 12
        elif period == "午前" and hour == 12:
            hour = 0

    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def parse_calendar_request(text: str, now: datetime | None = None):
    """読み取れたら calendar_add の引数を返す。確実に読めなければ None を返す。"""

    stripped = text.strip().translate(_DIGITS)

    if not is_simple(stripped):
        return None

    match = next((m for m in (p.match(stripped) for p in _PATTERNS) if m), None)

    if match is None:
        return None

    groups = match.groupdict()
    title = (groups.get("title") or groups.get("plain") or "").strip()
    time_value = _resolve_time(groups)

    if not title or time_value is None:
        return None

    try:
        day = _resolve_date(groups, now or datetime.now())
    except ValueError:
        return None

    if day < (now or datetime.now()).date():
        return None

    return {
        "title": title,
        "date": day.strftime("%Y/%m/%d"),
        "time": f"{time_value[0]:02d}:{time_value[1]:02d}",
    }
