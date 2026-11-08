"""実行前の承認フローの回帰テスト(OS操作とTelegram通信はモックする)。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CallbackQueryHandler

import main
from helpers import run_and_drain
from agent.models import Plan, Step
from agent.schemas import plan_requires_approval
from approvals import ApprovalStore

USER = 111
OTHER = 222
SECRET_BODY = "本文の全文"

MAIL_PLAN = Plan(
    steps=[Step(
        tool="mail_send",
        args={"to": "user@example.com", "subject": "件名", "body": SECRET_BODY},
        reason="依頼のため",
    )],
    message="送信しました",
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def env(monkeypatch, clock):
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(main.tm, "execute_plan", execute)
    monkeypatch.setattr(main.agent, "plan", lambda text: MAIL_PLAN)
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main, "approvals", ApprovalStore(ttl_seconds=600, clock=clock))
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    return SimpleNamespace(execute=execute)


def _message():
    return SimpleNamespace(text="依頼", chat_id=USER, reply_text=AsyncMock(), reply_photo=AsyncMock())


def _text_update(user_id=USER):
    return SimpleNamespace(
        message=_message(), effective_user=SimpleNamespace(id=user_id)
    )


def _buttons(update):
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    approve, reject = markup.inline_keyboard[0]
    return approve.callback_data, reject.callback_data


def _press(data, user_id=USER):
    query = SimpleNamespace(
        data=data,
        message=_message(),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
    run_and_drain(main.on_approval_decision(update, None))
    return query


def _request():
    update = _text_update()
    run_and_drain(main.receive(update, None))
    return update


# ── どのツールで承認が必要か ──

@pytest.mark.parametrize("tool, expected", [
    ("mail_send", True),
    ("calendar_add", True),
    ("close_app", True),
    ("send_to_vscode_chat", True),
    ("open_app", False),
    ("open_url", False),
    ("open_bookmark", False),
    ("excel_open", False),
    ("take_screenshot", False),
    ("google_search", True),
])
def test_approval_requirement_per_tool(tool, expected):
    plan = Plan(steps=[Step(tool=tool)])
    assert plan_requires_approval(plan, main.tm.registry) is expected


def test_unknown_tool_is_not_treated_as_safe():
    assert plan_requires_approval(Plan(steps=[Step(tool="未登録")]), main.tm.registry) is True


def test_mixed_plan_requires_approval_for_whole_plan():
    plan = Plan(steps=[Step(tool="take_screenshot"), Step(tool="mail_send")])
    assert plan_requires_approval(plan, main.tm.registry) is True


def test_plan_without_steps_needs_no_approval():
    assert plan_requires_approval(Plan(message="こんにちは"), main.tm.registry) is False


# ── 承認の流れ ──

def test_nothing_runs_until_approved_and_preview_shows_all_args(env):
    update = _request()
    env.execute.assert_not_called()
    preview = update.message.reply_text.call_args.args[0]
    assert "user@example.com" in preview
    assert "件名" in preview
    assert SECRET_BODY in preview


def test_callback_data_fits_telegram_limit(env):
    for data in _buttons(_request()):
        assert len(data.encode()) <= 64


def test_approve_runs_plan_once_and_removes_buttons(env):
    approve, _ = _buttons(_request())
    query = _press(approve)
    env.execute.assert_called_once_with(MAIL_PLAN)
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query.message.reply_text.assert_awaited_once_with("送信しました")


def test_double_press_does_not_run_twice(env):
    approve, _ = _buttons(_request())
    _press(approve)
    second = _press(approve)
    env.execute.assert_called_once()
    assert "実行していません" in second.message.reply_text.call_args.args[0]


def test_reject_does_not_run_and_cannot_be_approved_later(env):
    approve, reject = _buttons(_request())
    query = _press(reject)
    env.execute.assert_not_called()
    assert "中止しました" in query.message.reply_text.call_args.args[0]
    _press(approve)
    env.execute.assert_not_called()


def test_other_user_cannot_approve_and_owner_still_can(env, monkeypatch):
    approve, _ = _buttons(_request())
    query = _press(approve, user_id=OTHER)
    env.execute.assert_not_called()
    query.message.reply_text.assert_not_called()
    _press(approve)
    env.execute.assert_called_once()


def test_allowed_user_cannot_approve_someone_elses_request(env, monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER, OTHER}))
    approve, _ = _buttons(_request())
    _press(approve, user_id=OTHER)
    env.execute.assert_not_called()


def test_expired_request_does_not_run(env, clock):
    approve, _ = _buttons(_request())
    clock.now = 600
    query = _press(approve)
    env.execute.assert_not_called()
    assert "期限切れ" in query.message.reply_text.call_args.args[0]


def test_forged_id_does_not_run(env):
    _request()
    _press("approve:forged")
    env.execute.assert_not_called()


def test_execution_failure_is_reported(env):
    env.execute.side_effect = RuntimeError("boom")
    approve, _ = _buttons(_request())
    query = _press(approve)
    assert "boom" in query.message.reply_text.call_args.args[0]
    assert "送信しました" not in query.message.reply_text.call_args.args[0]


def test_button_update_failure_does_not_block_decision(env):
    approve, _ = _buttons(_request())
    query = SimpleNamespace(
        data=approve,
        message=_message(),
        answer=AsyncMock(side_effect=RuntimeError("network")),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER))
    run_and_drain(main.on_approval_decision(update, None))
    env.execute.assert_called_once()


def test_long_preview_is_split_with_buttons_only_on_last(env, monkeypatch):
    long_plan = Plan(steps=[Step(tool="mail_send", args={"body": "あ" * 9000})], message="ok")
    monkeypatch.setattr(main.agent, "plan", lambda text: long_plan)
    update = _request()
    calls = [c for c in update.message.reply_text.call_args_list if not c.args[0].startswith("⏳")]
    assert len(calls) == 3
    assert all(len(c.args[0]) <= main.MAX_MESSAGE_LENGTH for c in calls)
    assert "".join(c.args[0] for c in calls).count("あ") == 9000
    assert [("reply_markup" in c.kwargs) for c in calls] == [False, False, True]


def test_delayed_plan_is_scheduled_only_after_approval(env, monkeypatch):
    delayed = Plan(steps=[Step(tool="mail_send")], message="送信しました", delay=30)
    monkeypatch.setattr(main.agent, "plan", lambda text: delayed)
    schedule = MagicMock()
    monkeypatch.setattr(main.scheduler, "schedule", schedule)

    approve, _ = _buttons(_request())
    schedule.assert_not_called()

    query = _press(approve)
    assert schedule.call_args.args[0] == 30
    env.execute.assert_not_called()
    assert "30秒後" in query.message.reply_text.call_args.args[0]


def test_safe_tool_runs_without_buttons(env, monkeypatch):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok")
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    update = _request()
    env.execute.assert_called_once_with(plan)
    assert all("reply_markup" not in c.kwargs for c in update.message.reply_text.call_args_list)


def test_approval_is_recorded_in_history_only_after_running(env, monkeypatch):
    add = MagicMock()
    monkeypatch.setattr(main.app_context, "add", add)
    _, reject = _buttons(_request())
    _press(reject)
    add.assert_not_called()
    approve, _ = _buttons(_request())
    _press(approve)
    add.assert_called_once_with("依頼", MAIL_PLAN)


def test_application_registers_approval_handler(monkeypatch):
    monkeypatch.setattr(main, "TOKEN", "123:abc")
    app = main.build_application()
    handlers = [h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler)]
    assert len(handlers) == 1
    assert handlers[0].pattern.match("approve:x")
    assert handlers[0].pattern.match("reject:x")
    assert not handlers[0].pattern.match("other:x")


# ── ApprovalStore ──

def test_expired_entries_are_removed_on_add(clock):
    store = ApprovalStore(ttl_seconds=10, clock=clock)
    store.add("old", "t", USER)
    clock.now = 10
    store.add("new", "t", USER)
    assert [p.plan for p in store._pending.values()] == ["new"]
