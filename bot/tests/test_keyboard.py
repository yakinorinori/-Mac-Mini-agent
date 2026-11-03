import random

from core.keyboard import HumanTyping, compute_typing_params, to_romaji

CONFIG = {
    "base_wpm": 220,
    "wpm_jitter_ratio": 0.25,
    "think_pause_probability": 0.1,
    "think_pause_range": [0.0, 0.0],
    "word_boundary_extra_range": [0.0, 0.0],
    "punctuation_extra_range": [0.0, 0.0],
    "ime_commit_pre_wait_range": [0.0, 0.0],
    "ime_commit_post_wait_range": [0.0, 0.0],
    "mistake": {
        "notice_delay_chars_range": [1, 2],
        "types": ["adjacent_key", "swap", "omission"],
    },
}


def test_to_romaji_basic_conversion():
    assert to_romaji("こんにちは") == "konnnichiha"
    assert to_romaji("がっこう") == "gakkou"


def test_mistake_count_matches_requested_n():
    rng = random.Random(1)
    params = compute_typing_params("こんにちは", n_mistakes=2, config=CONFIG, rng=rng)
    assert len(params.mistakes) == 2


def test_mistake_positions_are_distinct():
    rng = random.Random(2)
    params = compute_typing_params("ありがとうございます", n_mistakes=3, config=CONFIG, rng=rng)
    positions = [m.position for m in params.mistakes]
    assert len(positions) == len(set(positions))


def test_mistake_count_capped_by_text_length():
    rng = random.Random(3)
    params = compute_typing_params("あ", n_mistakes=5, config=CONFIG, rng=rng)
    assert len(params.mistakes) <= 1


def test_type_text_calls_press_key_without_gui(monkeypatch):
    pressed = []

    monkeypatch.setattr(HumanTyping, "_press_key", lambda self, ch: pressed.append(ch))
    monkeypatch.setattr("core.keyboard.time.sleep", lambda *_: None)

    text = "こんにちは"
    rng = random.Random(4)
    params = compute_typing_params(text, n_mistakes=1, config=CONFIG, rng=rng)

    HumanTyping().type_text(text, params, CONFIG)

    assert "enter" in pressed
    assert len(pressed) > len(to_romaji(text))
