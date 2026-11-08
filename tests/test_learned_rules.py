"""2-3 学習ルールの提案、採用、取り消しの回帰テスト(LLMとTelegramはモックする)。"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import User
from telegram.ext import CallbackQueryHandler, CommandHandler

import config
import main
from agent.learned_rules import level_for_model, normalize_key
from agent.models import Intent, Plan, Step
from agent.planner import Planner
from agent.schemas import plan_requires_approval
from context import Context
from store import Store

USER = 111
OTHER = 222
EASY = config.MODEL_ROUTER["easy"]
MEDIUM = config.MODEL_ROUTER["medium"]
HARD = config.MODEL_ROUTER["hard"]


# ── キーの作り方 ──

@pytest.mark.parametrize("text, key", [
    ("画面ちょうだい", "画面ちょうだい"),
    ("  画面ちょうだい。", "画面ちょうだい"),
    ("ＴｅｘｔＥｄｉｔを開いて", "texteditを開いて"),
    ("「議事録」をまとめて", "「」をまとめて"),
    ("3分後にスクショ", "0分後にスクショ"),
])
def test_normalize_key(text, key):
    assert normalize_key(text) == key


@pytest.mark.parametrize("text", [None, "", "予定を調べて、そのあと連絡して", "もし空いていたら追加して"])
def test_complex_or_empty_text_has_no_key(text):
    assert normalize_key(text) is None


def test_level_for_model():
    assert level_for_model(EASY) == "easy"
    assert level_for_model(MEDIUM) == "medium"
    assert level_for_model("unknown") is None
    assert level_for_model("") is None


# ── 候補の判定 ──

def _run(store, text, model=EASY, status="succeeded", route="classify:hard"):
    store.record_run(Plan(steps=[Step(tool="take_screenshot")], route=route, model=model), status, 1, text)


def test_three_successes_with_same_model_make_a_candidate():
    store = Store()
    for _ in range(3):
        _run(store, "画面ちょうだい")
    assert store.rule_candidate("画面ちょうだい", 0, 3) == "easy"


def test_success_after_fallback_counts_for_the_model_that_worked():
    store = Store()
    for _ in range(3):
        _run(store, "画面ちょうだい", model=MEDIUM, route="classify:hard")
    assert store.rule_candidate("画面ちょうだい", 0, 3) == "medium"


@pytest.mark.parametrize("extra", [
    {"status": "failed"},
    {"status": "rejected"},
    {"status": "no_steps", "model": ""},
    {"model": MEDIUM},
])
def test_any_failure_or_mixed_models_blocks_candidate(extra):
    store = Store()
    for _ in range(3):
        _run(store, "画面ちょうだい")
    _run(store, "画面ちょうだい", **extra)
    assert store.rule_candidate("画面ちょうだい", 0, 3) is None


def test_too_few_or_old_successes_are_not_candidates():
    store = Store()
    for _ in range(2):
        _run(store, "画面ちょうだい")
    assert store.rule_candidate("画面ちょうだい", 0, 3) is None
    _run(store, "画面ちょうだい")
    assert store.rule_candidate("画面ちょうだい", time.time() + 10, 3) is None


def test_rule_routes_do_not_count():
    store = Store()
    for _ in range(3):
        _run(store, "画面ちょうだい", route="rule:easy")
    assert store.rule_candidate("画面ちょうだい", 0, 3) is None


def test_sensitive_requests_never_become_candidates():
    store = Store()
    for _ in range(5):
        store.record_run(
            Plan(steps=[Step(tool="mail_send")], route="classify:medium", model=MEDIUM),
            "succeeded", 1, "田中さんにメールして",
        )
    assert all(run["rule_key"] is None for run in store.recent_runs())


# ── 提案、採用、削除 ──

def test_propose_only_once_and_decide():
    store = Store()
    rule_id = store.propose_rule("画面ちょうだい", "easy")
    assert rule_id is not None
    assert store.propose_rule("画面ちょうだい", "easy") is None
    assert store.learned_level("画面ちょうだい") is None
    assert store.decide_rule(rule_id, accept=True) == ("画面ちょうだい", "easy")
    assert store.decide_rule(rule_id, accept=True) is None
    assert store.learned_level("画面ちょうだい。") == "easy"


def test_declined_rule_is_not_proposed_again():
    store = Store()
    rule_id = store.propose_rule("画面ちょうだい", "easy")
    store.decide_rule(rule_id, accept=False)
    assert store.learned_level("画面ちょうだい") is None
    assert store.propose_rule("画面ちょうだい", "easy") is None


@pytest.mark.parametrize("level", ["impossible", "api", "", None])
def test_only_model_levels_can_be_rules(level):
    assert Store().propose_rule("x", level) is None


def test_removed_rule_can_be_proposed_later():
    store = Store()
    rule_id = store.propose_rule("画面ちょうだい", "easy")
    store.decide_rule(rule_id, accept=True)
    assert store.remove_rule(rule_id=rule_id)
    assert store.learned_level("画面ちょうだい") is None
    assert store.propose_rule("画面ちょうだい", "easy") is not None


# ── Planner ──

def _planner(lookup):
    reply = json.dumps({"steps": [{"tool": "take_screenshot", "args": {}}], "message": "ok"})
    create = MagicMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
    ))
    planner = Planner(Context(), registry=main.tm.registry, rule_lookup=lookup)
    planner._classify = MagicMock(return_value="hard")
    planner._ollama_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return planner, create


def test_learned_rule_skips_classifier():
    planner, create = _planner(lambda text: "easy")
    plan = planner.plan(Intent("画面ちょうだい"))
    planner._classify.assert_not_called()
    assert create.call_args.kwargs["model"] == EASY
    assert (plan.route, plan.model) == ("learned:easy", EASY)


def test_without_learned_rule_classifier_is_used():
    planner, create = _planner(lambda text: None)
    plan = planner.plan(Intent("画面ちょうだい"))
    planner._classify.assert_called_once()
    assert plan.route == "classify:hard"


def test_fixed_rules_take_priority_over_learned_rules():
    lookup = MagicMock(return_value="hard")
    planner, create = _planner(lookup)
    plan = planner.plan(Intent("Chromeを開いて"))
    lookup.assert_not_called()
    assert plan.route == "rule:easy"


def test_learned_rule_does_not_skip_approval():
    planner, create = _planner(lambda text: "medium")
    reply = {"steps": [{"tool": "calendar_add", "args": {"title": "t", "date": "2099/01/01"}}], "message": "ok"}
    create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])
    plan = planner.plan(Intent("いつもの予定"))
    assert plan_requires_approval(plan, main.tm.registry) is True


# ── Botでの提案と取り消し ──

def _message():
    return SimpleNamespace(chat_id=USER, reply_text=AsyncMock(), reply_photo=AsyncMock())


def _update(text):
    return SimpleNamespace(message=SimpleNamespace(text=text, **vars(_message())), effective_user=SimpleNamespace(id=USER))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(main.tm, "execute_plan", execute)
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    return execute


def _send(text):
    update = _update(text)

    async def scenario():
        await main.receive(update, None)
        await main.jobs.join()

    asyncio.run(scenario())
    return update


def _buttons(update):
    for call in update.message.reply_text.call_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup and markup.inline_keyboard[0][0].callback_data.startswith("rule_"):
            return [button.callback_data for button in markup.inline_keyboard[0]], call.args[0]
    return None, None


def _press(data, user_id=USER):
    query = SimpleNamespace(data=data, message=_message(), answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    asyncio.run(main.on_rule_decision(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id)), None))
    return query


def _classified(monkeypatch, model=EASY):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok", route="classify:hard", model=model)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)


def test_third_success_proposes_once_and_yes_activates(env, monkeypatch):
    _classified(monkeypatch)
    assert _buttons(_send("画面ちょうだい"))[0] is None
    assert _buttons(_send("画面ちょうだい"))[0] is None
    data, text = _buttons(_send("画面ちょうだい"))
    assert data and data[0].startswith("rule_yes:")
    assert "画面ちょうだい" in text and EASY in text
    assert _buttons(_send("画面ちょうだい"))[0] is None

    query = _press(data[0])
    assert "追加しました" in query.message.reply_text.call_args.args[0]
    assert main.store.learned_level("画面ちょうだい") == "easy"
    assert "追加しました" not in _press(data[0]).message.reply_text.call_args.args[0]


def test_no_declines(env, monkeypatch):
    _classified(monkeypatch)
    for _ in range(2):
        _send("画面ちょうだい")
    data, _ = _buttons(_send("画面ちょうだい"))
    _press(data[1])
    assert main.store.learned_level("画面ちょうだい") is None


def test_other_user_cannot_decide(env, monkeypatch):
    _classified(monkeypatch)
    for _ in range(2):
        _send("画面ちょうだい")
    data, _ = _buttons(_send("画面ちょうだい"))
    query = _press(data[0], user_id=OTHER)
    query.message.reply_text.assert_not_called()
    assert main.store.learned_level("画面ちょうだい") is None


def test_rule_routed_requests_are_not_proposed(env, monkeypatch):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok", route="rule:easy", model=EASY)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    for _ in range(4):
        assert _buttons(_send("画面ちょうだい"))[0] is None


def _activate(key="画面ちょうだい", level="easy"):
    rule_id = main.store.propose_rule(key, level)
    main.store.decide_rule(rule_id, accept=True)
    return rule_id


def test_failure_on_learned_route_removes_rule(env, monkeypatch):
    _activate()
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok", route="learned:easy", model=EASY)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    env.side_effect = RuntimeError("boom")
    update = _send("画面ちょうだい")
    assert main.store.learned_level("画面ちょうだい") is None
    assert any("外しました" in c.args[0] for c in update.message.reply_text.call_args_list)


def test_rejection_on_learned_route_removes_rule(env, monkeypatch):
    _activate(key="いつもの予定", level="medium")
    plan = Plan(steps=[Step(tool="calendar_add", args={"title": "t", "date": "2099/01/01"})],
                message="ok", route="learned:medium", model=MEDIUM)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    update = _send("いつもの予定")
    reject = update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data
    query = SimpleNamespace(data=reject, message=_message(), answer=AsyncMock(), edit_message_reply_markup=AsyncMock())

    async def scenario():
        await main.on_approval_decision(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER)), None)

    asyncio.run(scenario())
    assert main.store.learned_level("いつもの予定") is None


def test_failure_on_classified_route_keeps_other_rules(env, monkeypatch):
    _activate()
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok", route="classify:easy", model=EASY)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    env.side_effect = RuntimeError("boom")
    _send("画面ちょうだい")
    assert main.store.learned_level("画面ちょうだい") == "easy"


def test_rules_command_lists_and_deletes(env):
    update = _update("/rules")
    asyncio.run(main.show_rules(update, None))
    assert "ありません" in update.message.reply_text.call_args.args[0]

    rule_id = _activate()
    update = _update("/rules")
    asyncio.run(main.show_rules(update, None))
    call = update.message.reply_text.call_args
    assert "画面ちょうだい" in call.args[0] and "easy" in call.args[0]
    data = call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert data == f"rule_del:{rule_id}"
    _press(data)
    assert main.store.learned_level("画面ちょうだい") is None


def test_handlers_are_registered(monkeypatch):
    monkeypatch.setattr(main, "TOKEN", "123:abc")
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    app = main.build_application()
    handlers = app.handlers[0]
    assert any(isinstance(h, CommandHandler) and "rules" in h.commands for h in handlers)
    rule = next(h for h in handlers if isinstance(h, CallbackQueryHandler) and h.callback is main.on_rule_decision)
    assert rule.pattern.match("rule_yes:12")
    assert not rule.pattern.match("rule_yes:0")
    assert not rule.pattern.match("rule_yes:abc")
    assert not rule.pattern.match("approve:x")


def test_old_database_gets_rule_key_column(tmp_path):
    import sqlite3
    path = tmp_path / "agent.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,"
               " tools TEXT NOT NULL, status TEXT NOT NULL, seconds REAL NOT NULL)")
    db.commit()
    db.close()
    store = Store(path)
    _run(store, "画面ちょうだい")
    assert store.recent_runs()[0]["rule_key"] == "画面ちょうだい"
