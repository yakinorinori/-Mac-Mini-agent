"""2-2 実行ログの回帰テスト。"""
import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from agent.models import Intent, Plan, Step
from agent.planner import Planner
from agent.schemas import PlanValidationError, build_plan
from context import Context
from ops import bot
from store import Store, loggable_text, summarize_runs

USER = 111
SECRET = "秘密の本文"


# ── Plannerが付ける経路とモデル ──

def _planner(reply=None, classify="easy"):
    content = reply or json.dumps({"steps": [{"tool": "open_app", "args": {"name": "Notes"}}], "message": "ok"})
    create = MagicMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    ))
    planner = Planner(Context(), registry=main.tm.registry)
    planner._classify = MagicMock(return_value=classify)
    planner._ollama_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return planner, create


@pytest.mark.parametrize("text, route, model", [
    ("今の画面のスクショを送って", "rule:screenshot", ""),
    ("今日の天気を調べて", "rule:search", ""),
    ("カレンダーに「会議」を2099年1月5日の10時に追加して", "rule:calendar", ""),
    ("Chromeを開いて", "rule:easy", "qwen2.5:3b"),
    ("こんにちは", "classify:easy", "qwen2.5:3b"),
])
def test_plan_carries_route_and_model(text, route, model):
    planner, _ = _planner()
    plan = planner.plan(Intent(text))
    assert (plan.route, plan.model) == (route, model)
    assert plan.plan_seconds >= 0


def test_impossible_and_failed_routes():
    planner, _ = _planner(classify="impossible")
    assert planner.plan(Intent("空を飛んで")).route == "classify:impossible"


def test_failed_llm_route_is_marked(monkeypatch):
    from openai import APIConnectionError
    import httpx
    planner, create = _planner()
    create.side_effect = APIConnectionError(request=httpx.Request("POST", "http://localhost"))
    plan = planner.plan(Intent("こんにちは"))
    assert plan.route == "classify:easy:failed" and plan.steps == []


def test_llm_cannot_set_measurement_fields():
    for field in ("route", "model", "plan_seconds"):
        with pytest.raises(PlanValidationError):
            build_plan({"steps": [], field: "x"}, main.tm.registry)


# ── 依頼文を残すか ──

@pytest.mark.parametrize("text, plan, kept", [
    ("今日の天気を調べて", Plan(steps=[Step(tool="google_search")]), True),
    ("カレンダーに会議を入れて", Plan(steps=[Step(tool="calendar_add")]), True),
    ("メールを送って。本文は秘密", Plan(steps=[Step(tool="mail_send")]), False),
    ("田中さんに連絡して", Plan(steps=[Step(tool="mail_send")]), False),
    ("a@example.com に送って", None, False),
    ("VS Codeに\"直して\"と送って", Plan(steps=[Step(tool="send_to_vscode_chat")]), False),
    ("開発モード", None, False),
    ("", None, False),
])
def test_loggable_text(text, plan, kept):
    assert (loggable_text(text, plan) is not None) is kept


def test_long_text_is_truncated():
    assert len(loggable_text("あ" * 500, None)) == 200


# ── 保存と移行 ──

def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "agent.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,"
                " tools TEXT NOT NULL, status TEXT NOT NULL, seconds REAL NOT NULL)")
    old.execute("INSERT INTO runs (time, tools, status, seconds) VALUES (?, '[]', 'succeeded', 1)", (time.time(),))
    old.commit()
    old.close()

    store = Store(path)
    store.record_run(Plan(steps=[Step(tool="take_screenshot")], route="rule:screenshot"), "succeeded", 0.5)
    runs = store.recent_runs()
    assert runs[0]["route"] == "rule:screenshot"
    assert runs[1]["route"] == ""


def test_mail_run_keeps_no_text_or_arguments(tmp_path):
    path = tmp_path / "agent.db"
    plan = Plan(steps=[Step(tool="mail_send", args={"body": SECRET})], route="rule:medium", model="qwen2.5:7b")
    Store(path).record_run(plan, "succeeded", 1, text=f"メールを送って。本文は{SECRET}")
    dump = json.dumps(sqlite3.connect(path).execute("SELECT * FROM runs").fetchall(), ensure_ascii=False)
    assert SECRET not in dump
    assert "qwen2.5:7b" in dump


def test_summary_groups_by_route(tmp_path):
    path = tmp_path / "agent.db"
    store = Store(path)
    fast = Plan(steps=[Step(tool="take_screenshot")], route="rule:screenshot")
    slow = Plan(steps=[Step(tool="calendar_add")], route="rule:medium", model="qwen2.5:7b", plan_seconds=20)
    store.record_run(fast, "succeeded", 2)
    store.record_run(fast, "succeeded", 4)
    store.record_run(slow, "failed", 120)
    rows = {r["route"]: r for r in summarize_runs(path, since=0)}
    assert rows["rule:screenshot"]["count"] == 2
    assert rows["rule:screenshot"]["run_seconds"] == 3
    assert rows["rule:medium"]["plan_seconds"] == 20
    assert rows["rule:medium"]["failed"] == 1


def test_summary_ignores_old_rows_and_missing_file(tmp_path):
    path = tmp_path / "agent.db"
    Store(path).record_run(Plan(route="x"), "succeeded", 1)
    assert summarize_runs(path, since=time.time() + 10) == []
    assert summarize_runs(tmp_path / "none.db", since=0) == []


def test_summary_opens_database_read_only(tmp_path, monkeypatch):
    path = tmp_path / "agent.db"
    Store(path)
    opened = []
    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        opened.append((str(target), kwargs.get("uri")))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    summarize_runs(path, since=0)
    assert opened == [(f"file:{path}?mode=ro", True)]


# ── mainでの記録 ──

def _update(text):
    return SimpleNamespace(
        message=SimpleNamespace(text=text, chat_id=USER, reply_text=AsyncMock(), reply_photo=AsyncMock()),
        effective_user=SimpleNamespace(id=USER),
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main.tm, "execute_plan", MagicMock(return_value=[]))


def _drain(coroutine):
    async def scenario():
        await coroutine
        await main.jobs.join()
    asyncio.run(scenario())


def test_successful_run_records_route_model_and_text(env, monkeypatch):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok", route="classify:easy", model="qwen2.5:3b", plan_seconds=3.2)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    _drain(main.receive(_update("画面ちょうだい"), None))
    run = main.store.recent_runs()[0]
    assert run["route"] == "classify:easy" and run["model"] == "qwen2.5:3b"
    assert run["plan_seconds"] == 3.2 and run["text"] == "画面ちょうだい"


def test_not_understood_and_no_steps_are_recorded(env, monkeypatch):
    monkeypatch.setattr(main.agent, "plan", lambda text: None)
    _drain(main.receive(_update("あいうえお"), None))
    monkeypatch.setattr(main.agent, "plan", lambda text: Plan(message="できません", route="classify:impossible"))
    _drain(main.receive(_update("空を飛んで"), None))
    statuses = [(r["status"], r["route"]) for r in main.store.recent_runs()]
    assert statuses == [("no_steps", "classify:impossible"), ("not_understood", "none")]
    main.tm.execute_plan.assert_not_called()


def test_rejected_approval_is_recorded(env, monkeypatch):
    plan = Plan(steps=[Step(tool="calendar_add", args={"title": "t", "date": "2099/01/01"})],
                message="ok", route="rule:calendar")
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    update = _update("カレンダーに入れて")
    _drain(main.receive(update, None))
    reject = update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data
    query = SimpleNamespace(data=reject, message=_update("").message, answer=AsyncMock(),
                            edit_message_reply_markup=AsyncMock())
    _drain(main.on_approval_decision(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER)), None))
    run = main.store.recent_runs()[0]
    assert (run["status"], run["route"]) == ("rejected", "rule:calendar")


# ── /stats ──

def test_stats_text():
    rows = [
        {"route": "rule:medium", "model": "qwen2.5:7b", "count": 3, "plan_seconds": 20.4, "run_seconds": 1.2, "failed": 1},
        {"route": "rule:screenshot", "model": "", "count": 5, "plan_seconds": 0, "run_seconds": 2.5, "failed": 0},
    ]
    text = bot.stats_text(rows)
    assert "rule:medium qwen2.5:7b: 3件 計画20.4 実行1.2 失敗1" in text
    assert "rule:screenshot: 5件 計画0.0 実行2.5" in text
    assert "記録はありません" in bot.stats_text([])


def test_stats_command_reads_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    Store(tmp_path / "agent.db").record_run(Plan(steps=[Step(tool="take_screenshot")], route="rule:screenshot"), "succeeded", 1)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(bot.show_stats(update, None))
    assert "rule:screenshot: 1件" in update.message.reply_text.call_args.args[0]
