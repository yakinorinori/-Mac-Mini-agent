"""カレンダー依頼の読み取りの回帰テスト。"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import main
from agent.calendar_rules import parse_calendar_request
from agent.models import Intent
from agent.planner import Planner
from agent.schemas import build_plan, plan_requires_approval
from context import Context

NOW = datetime(2026, 11, 4, 12, 0)


@pytest.mark.parametrize("text, title, day, time", [
    ("カレンダーに「動作確認テスト」という予定を2026年11月10日の14時に追加して", "動作確認テスト", "2026/11/10", "14:00"),
    ("カレンダーに「会議」を11月14日の10時に追加して", "会議", "2026/11/14", "10:00"),
    ("カレンダーに「会議」を11月14日10時30分に入れてください", "会議", "2026/11/14", "10:30"),
    ("カレンダーに「面談」を明日の9時半に登録して", "面談", "2026/11/05", "09:30"),
    ("明日の15時に打ち合わせをカレンダーに入れて", "打ち合わせ", "2026/11/05", "15:00"),
    ("明後日の午後3時に歯医者をカレンダーに追加して", "歯医者", "2026/11/06", "15:00"),
    ("今日の午前8時に朝礼をカレンダーに入れて", "朝礼", "2026/11/04", "08:00"),
    ("カレンダーに明日の15時に打ち合わせを追加して", "打ち合わせ", "2026/11/05", "15:00"),
    ("カレンダーに明日15時の会議を入れて", "会議", "2026/11/05", "15:00"),
    ("2026/12/1 18:30に忘年会をカレンダーに追加", "忘年会", "2026/12/01", "18:30"),
    ("カレンダーに「新年会」を1月5日の19時に追加して。", "新年会", "2027/01/05", "19:00"),
    ("カレンダーに「昼会」を明日の午後12時に追加して", "昼会", "2026/11/05", "12:00"),
    ("カレンダーに「深夜」を明日の午前12時に追加して", "深夜", "2026/11/05", "00:00"),
    ("カレンダーに「全角」を１１月１４日の１０時に追加して", "全角", "2026/11/14", "10:00"),
])
def test_common_forms_are_read(text, title, day, time):
    assert parse_calendar_request(text, NOW) == {"title": title, "date": day, "time": time}


@pytest.mark.parametrize("text", [
    "カレンダーに会議を入れて",
    "カレンダーに「会議」を明日追加して",
    "来週の金曜の10時に会議をカレンダーに追加して",
    "カレンダーに「会議」を13月1日の10時に追加して",
    "カレンダーに「会議」を2月30日の10時に追加して",
    "カレンダーに「会議」を明日の25時に追加して",
    "カレンダーに「会議」を明日の午後13時に追加して",
    "カレンダーに「会議」を明日の10時70分に追加して",
    "カレンダーに「会議」を2025年1月1日の10時に追加して",
    "カレンダーの予定を全部消して",
    "明日の15時に打ち合わせをカレンダーに入れて、そのあとメールも送って",
    "もし空いていたら明日の15時に会議をカレンダーに入れて",
    "カレンダーに「会議」を明日の10時に追加して\n別の依頼",
    "カレンダーに「会議」を明日の10時に追加できる？",
    "カレンダーに「" + "あ" * 90 + "」を明日の10時に追加して",
])
def test_uncertain_forms_go_to_llm(text):
    assert parse_calendar_request(text, NOW) is None


def _planner():
    create = MagicMock(side_effect=AssertionError("LLMを呼びました"))
    planner = Planner(Context(), registry=main.tm.registry)
    planner._classify = MagicMock(side_effect=AssertionError("分類を呼びました"))
    planner._ollama_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return planner


def test_calendar_rule_uses_no_llm_and_still_needs_approval(capsys):
    plan = _planner().plan(Intent("カレンダーに「秘密会議」を2099年1月5日の10時に追加して"))
    assert plan.steps[0].tool == "calendar_add"
    assert plan.steps[0].args == {"title": "秘密会議", "date": "2099/01/05", "time": "10:00"}
    assert plan.preapproved is False
    assert plan_requires_approval(plan, main.tm.registry) is True
    output = capsys.readouterr().out
    assert "秘密会議" not in output
    assert "経路=rule:calendar" in output


def test_parsed_arguments_satisfy_tool_schema():
    args = parse_calendar_request("明日の15時に打ち合わせをカレンダーに入れて", NOW)
    plan = build_plan({"steps": [{"tool": "calendar_add", "args": args}]}, main.tm.registry)
    assert plan.steps[0].args == args


def test_preview_shows_what_was_read():
    plan = _planner().plan(Intent("カレンダーに「会議」を2099年1月5日の10時に追加して"))
    preview = plan.describe()
    assert "会議" in preview and "2099/01/05" in preview and "10:00" in preview
