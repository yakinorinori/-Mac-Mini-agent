"""単純な依頼をルールで振り分け、LLMの難易度分類を省く。

語だけでは決めない。複数の手順や判断を含みそうな依頼は None を返し、通常の分類へ回す。
"""
import re

MAX_SIMPLE_LENGTH = 80

_COMPLEX_MARKERS = (
    "それから", "そのあと", "その後", "あと", "もし", "場合", "全部", "すべて", "全て",
    "毎", "重な", "調整", "確認して", "連絡", "教えて", "方法", "やり方", "とは",
    "?", "？", "\n", "て、", "して。",
)
_SCREENSHOT = re.compile(
    r"^(?:今の|いまの)?(?:画面の?)?(?:スクショ|スクリーンショット)を?"
    r"(?:撮って|とって|送って|見せて|お願い)?(?:ください|下さい|します)?[。！!]*$"
)
_SHOW_SCREEN = re.compile(r"^(?:今の|いまの)?画面を?(?:送って|見せて)(?:ください|下さい)?[。！!]*$")
_CALENDAR_ADD = re.compile(r"カレンダー.*(?:追加|入れて|登録)")
_MAIL_SEND = re.compile(r"メール.*(?:送って|送信)")
_APP_ACTION = re.compile(r"(?:開いて|起動して|閉じて|終了して)(?:ください|下さい)?[。！!]*$")


def _is_simple(text: str) -> bool:
    return len(text) <= MAX_SIMPLE_LENGTH and not any(mark in text for mark in _COMPLEX_MARKERS)


def is_screenshot_request(text: str) -> bool:
    """LLMを通さずに実行できる、スクリーンショットだけの依頼かを判定する。"""

    stripped = text.strip()
    return bool(_SCREENSHOT.match(stripped) or _SHOW_SCREEN.match(stripped))


def rule_level(text: str):
    """担当モデルの難易度を返す。ルールで決められなければ None を返す。"""

    stripped = text.strip()

    if not _is_simple(stripped):
        return None

    if _CALENDAR_ADD.search(stripped) or _MAIL_SEND.search(stripped):
        return "medium"

    if _APP_ACTION.search(stripped):
        return "easy"

    return None
