"""実行記録から学ぶ振り分けルールの共通処理。

学習で変えるのは担当モデル（難易度）の選択だけ。承認や実行内容は変えない。
"""
import re
import unicodedata

from agent.routing import is_simple
from config import MODEL_ROUTER

LEVELS = ("easy", "medium", "hard")
MIN_SUCCESSES = 3
WINDOW_DAYS = 30


def normalize_key(text):
    """同じ形の依頼を同じキーにする。「」の中身と数字は区別しない。

    複数の手順や判断を含みそうな依頼は None を返し、学習の対象にしない。
    """

    if not text or not is_simple(text):
        return None

    key = unicodedata.normalize("NFKC", text).strip().lower()
    key = re.sub(r"「[^」]*」", "「」", key)
    key = re.sub(r"\d+", "0", key)
    key = re.sub(r"\s+", " ", key).rstrip("。.!！ ")

    return key or None


def level_for_model(model):
    """実際に成功したモデルに対応する、いちばん低い難易度を返す。"""

    for level in LEVELS:
        if model and MODEL_ROUTER.get(level) == model:
            return level

    return None
