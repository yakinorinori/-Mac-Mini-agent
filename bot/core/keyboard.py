"""人間らしいキーボード入力(HumanTyping)。ローマ字IME入力・打ち間違えを再現する。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum

# 基本的なひらがな→ローマ字テーブル(主要なものだけを収録。未収録文字はそのまま1文字扱い)
_ROMAJI_TABLE: dict[str, str] = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "wo", "ん": "nn",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo",
    "ー": "-", "、": ",", "。": ".", "　": " ",
}

# QWERTY配列での隣接キー(簡易版。誤打ちシミュレーション用)
_QWERTY_NEIGHBORS: dict[str, str] = {
    "q": "wa", "w": "qes", "e": "wrd", "r": "etf", "t": "ryg",
    "y": "tuh", "u": "yij", "i": "uok", "o": "ipl", "p": "ol",
    "a": "qsz", "s": "awdx", "d": "serfc", "f": "drtgv", "g": "ftyhb",
    "h": "gyujn", "j": "huikm", "k": "jiol", "l": "kop",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}

# US配列のキーコード対応表(主要キーのみ)
_KEYCODE_TABLE: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
    "n": 45, "m": 46,
    "enter": 36, "backspace": 51, "space": 49,
    ",": 43, ".": 47, "-": 27,
}


class MistakeType(str, Enum):
    ADJACENT_KEY = "adjacent_key"
    SWAP = "swap"
    OMISSION = "omission"


@dataclass(frozen=True)
class MistakeSpec:
    """乱数で確定済みの打ち間違え1件分の内容。"""
    position: int          # テキスト内の何文字目(ローマ字化前のインデックス)
    mistake_type: MistakeType
    notice_delay_chars: int


@dataclass(frozen=True)
class TypingParams:
    """乱数で確定済みのタイピングパラメータ。"""
    wpm: float
    mistakes: tuple[MistakeSpec, ...]
    think_pause_positions: tuple[int, ...]
    delay_seed: int


def to_romaji(text: str) -> str:
    """ひらがな文字列をローマ字に変換する(未収録文字はそのまま出力)。促音「っ」は次の子音を重ねる。"""
    result = []
    chars = list(text)
    i = 0
    while i < len(chars):
        ch = chars[i]

        if ch == "っ" and i + 1 < len(chars):
            nxt_romaji = _ROMAJI_TABLE.get(chars[i + 1], chars[i + 1])
            if nxt_romaji:
                result.append(nxt_romaji[0])
            i += 1
            continue

        result.append(_ROMAJI_TABLE.get(ch, ch))
        i += 1

    return "".join(result)


def compute_typing_params(text: str, n_mistakes: int, config: dict, rng: random.Random) -> TypingParams:
    """基準WPM・打ち間違え位置と種類・考え込みポーズ位置を乱数で決定する。"""
    wpm_jitter = config["wpm_jitter_ratio"]
    wpm = config["base_wpm"] * (1 + rng.uniform(-wpm_jitter, wpm_jitter))

    length = max(1, len(text))
    n_mistakes = min(n_mistakes, length)

    positions = sorted(rng.sample(range(length), n_mistakes)) if n_mistakes else []
    mistake_types = list(MistakeType)

    mistakes = tuple(
        MistakeSpec(
            position=pos,
            mistake_type=rng.choice(mistake_types),
            notice_delay_chars=rng.randint(*config["mistake"]["notice_delay_chars_range"]),
        )
        for pos in positions
    )

    n_pauses = max(0, round(length * config["think_pause_probability"]))
    remaining = [p for p in range(length) if p not in positions]
    pause_positions = tuple(sorted(rng.sample(remaining, min(n_pauses, len(remaining)))))

    return TypingParams(
        wpm=wpm,
        mistakes=mistakes,
        think_pause_positions=pause_positions,
        delay_seed=rng.randrange(2**31),
    )


def _char_delay(wpm: float, rng: random.Random) -> float:
    """1文字あたりの入力間隔を正規分布で決定する(基準WPMベース。1単語=5文字換算)。"""
    base_seconds_per_char = 60.0 / (wpm * 5)
    return max(0.02, rng.gauss(base_seconds_per_char, base_seconds_per_char * 0.3))


def _apply_mistake(rng: random.Random, correct_char: str, mistake_type: MistakeType) -> str:
    """指定された種類の誤打鍵文字を生成する(SWAPは呼び出し側で処理するためここでは対象外)。"""
    if mistake_type == MistakeType.ADJACENT_KEY:
        neighbors = _QWERTY_NEIGHBORS.get(correct_char.lower())
        return rng.choice(neighbors) if neighbors else correct_char
    if mistake_type == MistakeType.OMISSION:
        return ""
    return correct_char


class HumanTyping:
    """IMEローマ字入力・打ち間違え・修正動作を再現するキーボード実行部。"""

    def type_text(self, text: str, params: TypingParams, config: dict) -> None:
        romaji = to_romaji(text)
        rng = random.Random(params.delay_seed)

        mistake_by_pos = {m.position: m for m in params.mistakes}
        pause_positions = set(params.think_pause_positions)

        chars = list(romaji)
        i = 0
        while i < len(chars):
            if i in pause_positions:
                time.sleep(rng.uniform(*config["think_pause_range"]))

            mistake = mistake_by_pos.get(i)
            if mistake is not None:
                i = self._type_with_mistake(chars, i, mistake, params, config, rng)
                continue

            self._press_key(chars[i])
            time.sleep(_char_delay(params.wpm, rng))
            if chars[i] in ",.":
                time.sleep(rng.uniform(*config["punctuation_extra_range"]))
            i += 1

        time.sleep(rng.uniform(*config["ime_commit_pre_wait_range"]))
        self._press_key("enter")
        time.sleep(rng.uniform(*config["ime_commit_post_wait_range"]))

    def _type_with_mistake(
        self,
        chars: list[str],
        index: int,
        mistake: MistakeSpec,
        params: TypingParams,
        config: dict,
        rng: random.Random,
    ) -> int:
        """打ち間違え→n文字ぶん打ってから気づく→Backspaceで戻す→打ち直す、を実行する。"""
        correct_char = chars[index]

        if mistake.mistake_type == MistakeType.SWAP and index + 1 < len(chars):
            typed_sequence = [chars[index + 1], chars[index]]
            skip = 2
        else:
            wrong_char = _apply_mistake(rng, correct_char, mistake.mistake_type)
            typed_sequence = [wrong_char] if wrong_char else []
            skip = 1

        extra_chars = chars[index + skip: index + skip + mistake.notice_delay_chars]

        for ch in typed_sequence + extra_chars:
            if ch:
                self._press_key(ch)
                time.sleep(_char_delay(params.wpm, rng))

        n_backspace = len([c for c in typed_sequence if c]) + len(extra_chars)
        for _ in range(n_backspace):
            self._press_key("backspace")
            time.sleep(_char_delay(params.wpm, rng) * 0.6)

        correct_sequence = chars[index: index + skip] + extra_chars
        for ch in correct_sequence:
            self._press_key(ch)
            time.sleep(_char_delay(params.wpm, rng))

        return index + skip + len(extra_chars)

    def _press_key(self, char: str) -> None:
        keycode = _KEYCODE_TABLE.get(char.lower())
        if keycode is None:
            return

        from Quartz import CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap

        down = CGEventCreateKeyboardEvent(None, keycode, True)
        CGEventPost(kCGHIDEventTap, down)
        up = CGEventCreateKeyboardEvent(None, keycode, False)
        CGEventPost(kCGHIDEventTap, up)
