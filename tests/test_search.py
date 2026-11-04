"""Google検索の指示判定と承認の回帰テスト(OS操作はモックする)。"""
import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from helpers import run_and_drain
from agent.models import Intent, Plan, Step
from agent.planner import Planner, extract_search_query
from agent.schemas import PlanValidationError, build_plan, plan_requires_approval
from approvals import ApprovalStore
from context import Context
from tools.browser.google_search import google_search


@pytest.mark.parametrize("text, query", [
    ("Pythonチュートリアルを検索して", "Pythonチュートリアル"),
    ("今日の天気を調べて", "今日の天気"),
    ("東京の人口について調べてください", "東京の人口"),
    ("Googleで最新ニュースを検索", "最新ニュース"),
    ("最新ニュースをGoogleで検索して", "最新ニュース"),
    ("M1 Mac mini 8GB をググって", "M1 Mac mini 8GB"),
    ("Claude Codeを検索する。", "Claude Code"),
    ("検索 今日の天気", "今日の天気"),
    ("調べて　円相場", "円相場"),
])
def test_explicit_search_is_detected(text, query):
    assert extract_search_query(text) == query


@pytest.mark.parametrize("text", [
    "こんにちは",
    "天気",
    "検索",
    "検索する",
    "調べて",
    "カレンダーの予定を調べて",
    "メールを検索して",
    "画面を調べて",
    "このファイルを検索して",
    "検索機能を実装して",
    "1行目を検索して\n2行目",
])
def test_other_text_is_not_a_search(text):
    assert extract_search_query(text) is None


def _planner(replies=None):
    create = MagicMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])
        for r in (replies or [])
    ])
    planner = Planner(Context(), registry=main.tm.registry)
    planner._classify = MagicMock(return_value="easy")
    planner._ollama_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return planner, create


def test_keyword_search_skips_llm_and_needs_no_approval():
    planner, create = _planner()
    plan = planner.plan(Intent("今日の天気を調べて"))
    create.assert_not_called()
    planner._classify.assert_not_called()
    assert plan.steps[0].args == {"query": "今日の天気"}
    assert plan_requires_approval(plan, main.tm.registry) is False


def test_llm_chosen_search_requires_approval():
    reply = json.dumps({"steps": [{"tool": "google_search", "args": {"query": "挨拶"}}], "message": "ok"})
    planner, _ = _planner([reply])
    plan = planner.plan(Intent("こんにちは"))
    assert plan.steps[0].tool == "google_search"
    assert plan.preapproved is False
    assert plan_requires_approval(plan, main.tm.registry) is True


def test_llm_cannot_preapprove_its_own_plan():
    data = {"steps": [{"tool": "google_search", "args": {"query": "x"}}], "preapproved": True}
    with pytest.raises(PlanValidationError):
        build_plan(data, main.tm.registry)


def test_preapproved_flag_covers_only_google_search():
    plan = Plan(
        steps=[Step(tool="google_search"), Step(tool="mail_send")],
        preapproved=True,
    )
    assert plan_requires_approval(plan, main.tm.registry) is True


def test_search_opens_encoded_url(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    url = google_search("C++ & 天気?")
    assert url == "https://www.google.com/search?q=C%2B%2B+%26+%E5%A4%A9%E6%B0%97%3F"
    assert run.call_args.args[0] == ["open", url]


def _update(text):
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock(), reply_photo=AsyncMock()),
        effective_user=SimpleNamespace(id=111),
    )


def test_keyword_search_runs_and_replies_with_link(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main.agent.planner, "_classify", MagicMock(side_effect=AssertionError))
    update = _update("今日の天気を調べて")

    run_and_drain(main.receive(update, None))

    replies = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert run.call_args.args[0][0] == "open"
    assert any(r.startswith("https://www.google.com/search?q=") for r in replies)
    assert all("reply_markup" not in c.kwargs for c in update.message.reply_text.call_args_list)


def test_llm_search_waits_for_approval(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main, "approvals", ApprovalStore())
    plan = Plan(steps=[Step(tool="google_search", args={"query": "x"})], message="ok")
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    update = _update("こんにちは")

    run_and_drain(main.receive(update, None))

    run.assert_not_called()
    assert "reply_markup" in update.message.reply_text.call_args.kwargs


def test_repeat_of_keyword_search_stays_preapproved():
    planner, _ = _planner()
    plan = planner.plan(Intent("天気を調べて"))
    planner.context.add("天気を調べて", plan)
    assert planner.plan(Intent("もう一度")).preapproved is True
