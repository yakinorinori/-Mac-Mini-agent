"""ルールによる振り分けの回帰テスト(LLMはモックする)。"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
import main
from agent.models import Intent
from agent.planner import Planner
from agent.routing import is_screenshot_request, rule_level
from agent.schemas import plan_requires_approval
from context import Context


@pytest.mark.parametrize("text", [
    "スクショ",
    "スクショ撮って",
    "スクショを送って",
    "今の画面のスクリーンショットを送って",
    "いまの画面のスクショをお願いします。",
    "画面を見せて",
    "今の画面を送ってください",
])
def test_screenshot_requests_are_detected(text):
    assert is_screenshot_request(text)


@pytest.mark.parametrize("text", [
    "スクショの撮り方を教えて",
    "スクショを撮って田中さんに送って",
    "スクショ機能を実装して",
    "画面が固まった",
    "画面を閉じて",
])
def test_other_screen_text_is_not_a_screenshot(text):
    assert not is_screenshot_request(text)


@pytest.mark.parametrize("text, level", [
    ("カレンダーに「動作確認テスト」という予定を2026年11月10日の14時に追加して", "medium"),
    ("カレンダーに会議を入れて", "medium"),
    ("メールを送って。宛先は a@example.com、件名は テスト、本文は こんにちは", "medium"),
    ("Chromeを開いて", "easy"),
    ("メモを起動してください", "easy"),
    ("TextEditを閉じて", "easy"),
    ("カレンダーに会議を入れて。", "medium"),
])
def test_simple_requests_use_rules(text, level):
    assert rule_level(text) == level


@pytest.mark.parametrize("text", [
    "こんにちは",
    "来週の予定を調べて、重なりを直して、関係者に連絡して",
    "カレンダーの予定を全部消して",
    "カレンダーに会議を追加して、そのあとメールを送って",
    "もし空いていたらカレンダーに追加して",
    "メールの送り方を教えて",
    "毎週月曜の会議をカレンダーに追加して",
    "カレンダーに会議を追加できる？",
    "カレンダーに追加して\nメールも送って",
    "カレンダーに追加して。メールも送って",
    "カレンダーに" + "あ" * 80 + "を追加して",
    "Chromeの開き方を教えて",
])
def test_complex_requests_go_to_classifier(text):
    assert rule_level(text) is None


def _planner():
    reply = json.dumps({"steps": [{"tool": "open_app", "args": {"name": "Notes"}}], "message": "ok"})
    create = MagicMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
    ))
    planner = Planner(Context(), registry=main.tm.registry)
    planner._classify = MagicMock(return_value="hard")
    planner._ollama_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return planner, create


def test_screenshot_uses_no_llm():
    planner, create = _planner()
    plan = planner.plan(Intent("今の画面のスクリーンショットを送って"))
    create.assert_not_called()
    planner._classify.assert_not_called()
    assert plan.steps[0].tool == "take_screenshot"
    assert plan_requires_approval(plan, main.tm.registry) is False


def test_rule_skips_classifier_and_uses_rule_model():
    planner, create = _planner()
    planner.plan(Intent("カレンダーに会議を入れて"))
    planner._classify.assert_not_called()
    assert create.call_args.kwargs["model"] == config.MODEL_ROUTER["medium"]


def test_complex_request_still_uses_classifier():
    planner, create = _planner()
    planner.plan(Intent("来週の予定を調べて、重なりを直して"))
    planner._classify.assert_called_once()
    assert create.call_args.kwargs["model"] == config.MODEL_ROUTER["hard"]


def test_rule_routing_keeps_local_fallback():
    planner, create = _planner()
    create.side_effect = [RuntimeError("missing"), create.return_value]
    plan = planner.plan(Intent("カレンダーに会議を入れて"))
    models = [c.kwargs["model"] for c in create.call_args_list]
    assert models == [config.MODEL_ROUTER["medium"], config.MODEL_ROUTER["hard"]]
    assert plan.steps


def test_rule_routing_does_not_skip_approval():
    planner, create = _planner()
    reply = {"steps": [{"tool": "calendar_add", "args": {"title": "t", "date": "2026/11/10"}}], "message": "ok"}
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))]
    )
    plan = planner.plan(Intent("カレンダーに会議を入れて"))
    assert plan_requires_approval(plan, main.tm.registry) is True


def test_route_log_contains_no_message_text(capsys):
    planner, _ = _planner()
    planner.plan(Intent("カレンダーに秘密会議を入れて"))
    output = capsys.readouterr().out
    assert "秘密会議" not in output
    assert "経路=rule:medium" in output
    assert "所要=" in output
