"""SQLiteへの保存と再起動後の引き継ぎの回帰テスト。"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from agent.models import Plan, Step
from store import Store, StoredApprovals, plan_from_json, plan_to_json

USER = 111
OTHER = 222
SECRET = "秘密の本文"

MAIL = Plan(
    steps=[Step(tool="mail_send", args={"to": "a@example.com", "subject": "件名", "body": SECRET})],
    message="送信しました",
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_plan_round_trip():
    plan = Plan(steps=[Step(tool="google_search", args={"query": "天気"}, reason="r")],
                message="m", delay=30, preapproved=True)
    assert plan_from_json(plan_to_json(plan)) == plan


def test_database_file_is_private(tmp_path):
    Store(tmp_path / "state" / "agent.db")
    assert (tmp_path / "state" / "agent.db").stat().st_mode & 0o777 == 0o600


def test_approval_survives_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "agent.db"
    approval_id = StoredApprovals(Store(path, clock), clock=clock).add(MAIL, "依頼", USER)

    restarted = StoredApprovals(Store(path, clock), clock=clock)
    pending = restarted.take(approval_id, USER)

    assert pending.plan == MAIL and pending.text == "依頼"
    assert restarted.take(approval_id, USER) is None


def test_approval_rules(tmp_path):
    clock = Clock()
    approvals = StoredApprovals(Store(tmp_path / "a.db", clock), ttl_seconds=600, clock=clock)
    approval_id = approvals.add(MAIL, "依頼", USER)
    assert approvals.take(approval_id, OTHER) is None
    assert approvals.take("forged", USER) is None
    clock.now += 600
    assert approvals.take(approval_id, USER) is None


def test_expired_approvals_are_deleted(tmp_path):
    clock = Clock()
    path = tmp_path / "agent.db"
    StoredApprovals(Store(path, clock), ttl_seconds=10, clock=clock).add(MAIL, "依頼", USER)
    clock.now += 10
    StoredApprovals(Store(path, clock), clock=clock)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_scheduled_claim_and_cancel(tmp_path):
    store = Store(tmp_path / "agent.db")
    mine = store.add_scheduled(USER, USER, 100, "t", MAIL)
    other = store.add_scheduled(OTHER, OTHER, 100, "t", MAIL)
    assert store.cancel_scheduled(USER) == 1
    assert store.claim_scheduled(mine) is False
    assert store.claim_scheduled(other) is True
    assert store.claim_scheduled(other) is False
    assert store.list_scheduled() == []


def test_runs_keep_tool_names_only(tmp_path):
    path = tmp_path / "agent.db"
    store = Store(path)
    store.record_run(MAIL, "succeeded", 1.234)
    run = store.recent_runs()[0]
    assert (run["tools"], run["status"], run["seconds"]) == (["mail_send"], "succeeded", 1.23)
    rows = sqlite3.connect(path).execute("SELECT * FROM runs").fetchall()
    assert SECRET not in json.dumps(rows, ensure_ascii=False)


def test_old_runs_are_deleted(tmp_path):
    clock = Clock()
    path = tmp_path / "agent.db"
    Store(path, clock).record_run(MAIL, "succeeded", 1)
    clock.now += 91 * 86400
    assert Store(path, clock).recent_runs() == []


# ── main ──

def _update(text="依頼"):
    return SimpleNamespace(
        message=SimpleNamespace(text=text, chat_id=USER, reply_text=AsyncMock(), reply_photo=AsyncMock()),
        effective_user=SimpleNamespace(id=USER),
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(main.tm, "execute_plan", execute)
    monkeypatch.setattr(main, "scheduled_timers", {})
    return execute


def test_runs_are_recorded_for_success_and_failure(env, monkeypatch):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok")
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)

    async def scenario():
        await main.receive(_update(), None)
        await main.jobs.join()
        env.side_effect = RuntimeError("boom")
        await main.receive(_update(), None)
        await main.jobs.join()

    asyncio.run(scenario())
    assert [r["status"] for r in main.store.recent_runs()] == ["failed", "succeeded"]


def test_delayed_plan_is_stored_and_cancel_removes_it(env, monkeypatch):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="ok", delay=600)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    cancel = _update("/cancel")

    async def scenario():
        await main.receive(_update(), None)
        await main.jobs.join()
        assert len(main.store.list_scheduled()) == 1
        await main.cancel(cancel, None)

    asyncio.run(scenario())
    assert main.store.list_scheduled() == []
    assert "遅延実行 1件" in cancel.message.reply_text.call_args.args[0]
    env.assert_not_called()


def test_fire_after_cancel_does_not_run(env, monkeypatch):
    callbacks = []
    monkeypatch.setattr(main.scheduler, "schedule", lambda seconds, callback: callbacks.append(callback))
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())

    async def scenario():
        job_id = main.store.add_scheduled(USER, USER, 0, "t", MAIL)
        main.schedule_stored(job_id, 1, MAIL, message, USER, asyncio.get_running_loop())
        main.store.cancel_scheduled(USER)
        await asyncio.to_thread(callbacks[0])
        await main.jobs.join()

    asyncio.run(scenario())
    env.assert_not_called()


def test_restart_restores_schedules_and_drops_stale_ones(env, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(main.time, "time", lambda: now)
    scheduled = []
    monkeypatch.setattr(main.scheduler, "schedule", lambda seconds, callback: scheduled.append((seconds, callback)))
    plan = Plan(steps=[Step(tool="take_screenshot")], message="遅延完了", delay=60)
    main.store.add_scheduled(USER, USER, now + 120, "future", plan)
    main.store.add_scheduled(USER, USER, now - 30, "slightly late", plan)
    main.store.add_scheduled(USER, USER, now - main.LATE_LIMIT_SECONDS - 1, "stale", plan)
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())

    async def scenario():
        await main.restore_scheduled(bot)
        for _, callback in scheduled:
            await asyncio.to_thread(callback)
        await main.jobs.join()

    asyncio.run(scenario())
    assert sorted(seconds for seconds, _ in scheduled) == [0, 120]
    assert env.call_count == 2
    assert main.store.list_scheduled() == []
    notice = bot.send_message.call_args_list[0].kwargs["text"]
    assert "2件引き継ぎました" in notice and "1件取り消しました" in notice
    results = [c.kwargs for c in bot.send_message.call_args_list[1:]]
    assert all(r["chat_id"] == USER and r["text"] == "遅延完了" for r in results)


def test_restore_with_nothing_sends_nothing(env):
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(main.restore_scheduled(bot))
    bot.send_message.assert_not_called()


def test_approval_after_restart_runs_plan(env, monkeypatch, tmp_path):
    from store import open_store
    path_store = open_store(tmp_path)
    monkeypatch.setattr(main, "store", path_store)
    monkeypatch.setattr(main, "approvals", StoredApprovals(path_store))
    approval_id = main.approvals.add(Plan(steps=[Step(tool="take_screenshot")], message="ok"), "依頼", USER)

    restarted = open_store(tmp_path)
    monkeypatch.setattr(main, "store", restarted)
    monkeypatch.setattr(main, "approvals", StoredApprovals(restarted))
    query = SimpleNamespace(
        data=f"approve:{approval_id}", message=SimpleNamespace(chat_id=USER, reply_text=AsyncMock(), reply_photo=AsyncMock()),
        answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
    )
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))

    async def scenario():
        await main.on_approval_decision(
            SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER)), None,
        )
        await main.jobs.join()

    asyncio.run(scenario())
    env.assert_called_once()
