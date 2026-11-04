"""ジョブキューと /cancel の回帰テスト(OS操作とTelegram通信はモックする)。"""
import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler, MessageHandler

import main
from agent.models import Plan, Step
from helpers import run_and_drain
from jobs import JobQueue

USER = 111
OTHER = 222


def test_jobs_run_one_at_a_time_in_order():
    events = []

    async def job(name):
        events.append(f"{name}-start")
        await asyncio.sleep(0.01)
        events.append(f"{name}-end")

    async def scenario():
        queue = JobQueue()
        assert queue.submit("a", lambda: job("a"), USER) == 0
        assert queue.submit("b", lambda: job("b"), USER) == 1
        assert queue.submit("c", lambda: job("c"), USER) == 2
        await queue.join()

    asyncio.run(scenario())
    assert events == ["a-start", "a-end", "b-start", "b-end", "c-start", "c-end"]


def test_failed_job_does_not_stop_following_jobs(capsys):
    done = []

    async def bad():
        raise ValueError("secret detail")

    async def good():
        done.append(True)

    async def scenario():
        queue = JobQueue()
        queue.submit("bad", bad, USER)
        queue.submit("good", good, USER)
        await queue.join()

    asyncio.run(scenario())
    assert done == [True]
    assert "secret detail" not in capsys.readouterr().out


def test_cancel_removes_only_own_pending_jobs():
    ran = []

    async def job(name):
        ran.append(name)

    async def scenario():
        queue = JobQueue()
        queue.submit("mine1", lambda: job("mine1"), USER)
        queue.submit("other", lambda: job("other"), OTHER)
        queue.submit("mine2", lambda: job("mine2"), USER)
        assert queue.cancel_pending(USER) == ["mine1", "mine2"]
        await queue.join()

    asyncio.run(scenario())
    assert ran == ["other"]


def test_worker_restarts_after_queue_becomes_empty():
    ran = []

    async def job():
        ran.append(True)

    async def scenario():
        queue = JobQueue()
        queue.submit("a", job, USER)
        await queue.join()
        queue.submit("b", job, USER)
        await queue.join()

    asyncio.run(scenario())
    assert len(ran) == 2


def test_close_stops_pending_work():
    ran = []

    async def slow():
        await asyncio.sleep(10)

    async def job():
        ran.append(True)

    async def scenario():
        queue = JobQueue()
        queue.submit("slow", slow, USER)
        queue.submit("next", job, USER)
        await asyncio.sleep(0)
        queue.close()
        await queue.join()

    asyncio.run(scenario())
    assert ran == []


def _update(text, user_id=USER):
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock(), reply_photo=AsyncMock()),
        effective_user=SimpleNamespace(id=user_id),
    )


def _texts(update):
    return [c.args[0] for c in update.message.reply_text.call_args_list]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main, "jobs", JobQueue())
    monkeypatch.setattr(main, "scheduled_timers", {})
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(main.tm, "execute_plan", execute)
    return SimpleNamespace(execute=execute)


def _blocking_planner(monkeypatch, plan=None):
    """最初の計画だけ、解放されるまでスレッド内で止まるPlanner。"""
    release = threading.Event()
    calls = []

    def planner(text):
        calls.append(text)
        if len(calls) == 1:
            release.wait(5)
        return plan or Plan(steps=[Step(tool="take_screenshot")], message=f"完了:{text}")

    monkeypatch.setattr(main.agent, "plan", planner)
    return release, calls


async def _until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("条件を満たしませんでした")


def test_event_loop_stays_responsive_during_planning(env, monkeypatch):
    release, calls = _blocking_planner(monkeypatch)
    first, second = _update("一件目"), _update("二件目")

    async def scenario():
        await main.receive(first, None)
        await _until(lambda: calls == ["一件目"])
        # 計画中でも次のメッセージを受け付け、イベントループが動いている
        await asyncio.wait_for(main.receive(second, None), timeout=1)
        assert "前に1件" in _texts(second)[0]
        await asyncio.sleep(0.01)
        assert calls == ["一件目"]
        release.set()
        await main.jobs.join()

    asyncio.run(scenario())
    assert calls == ["一件目", "二件目"]
    assert env.execute.call_count == 2


def test_tool_execution_does_not_block_event_loop(env, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def execute(plan):
        started.set()
        release.wait(5)
        return []

    env.execute.side_effect = execute
    monkeypatch.setattr(main.agent, "plan", lambda text: Plan(steps=[Step(tool="take_screenshot")], message="ok"))
    update = _update("依頼")

    async def scenario():
        await main.receive(update, None)
        await _until(started.is_set)
        await asyncio.wait_for(asyncio.sleep(0), timeout=1)
        release.set()
        await main.jobs.join()

    asyncio.run(scenario())
    env.execute.assert_called_once()


def test_cancel_removes_waiting_message_and_reports_running(env, monkeypatch):
    release, calls = _blocking_planner(monkeypatch)
    first, second, cancel = _update("一件目"), _update("二件目"), _update("/cancel")

    async def scenario():
        await main.receive(first, None)
        await _until(lambda: calls == ["一件目"])
        await main.receive(second, None)
        await main.cancel(cancel, None)
        release.set()
        await main.jobs.join()

    asyncio.run(scenario())
    text = _texts(cancel)[0]
    assert "取り消しました: 二件目" in text
    assert "実行中の処理は止められません: 一件目" in text
    assert calls == ["一件目"]
    assert env.execute.call_count == 1


def test_cancel_with_nothing_to_cancel(env):
    update = _update("/cancel")
    asyncio.run(main.cancel(update, None))
    assert "取り消せる処理はありません" in _texts(update)[0]


def test_cancel_stops_scheduled_delayed_run(env, monkeypatch):
    monkeypatch.setattr(
        main.agent, "plan",
        lambda text: Plan(steps=[Step(tool="take_screenshot")], message="ok", delay=60),
    )
    update, cancel = _update("あとで"), _update("/cancel")

    async def scenario():
        await main.receive(update, None)
        await main.jobs.join()
        assert len(main.scheduled_timers) == 1
        timer = next(iter(main.scheduled_timers))
        await main.cancel(cancel, None)
        timer.join(2)
        assert timer.finished.is_set()

    asyncio.run(scenario())
    assert "遅延実行 1件" in _texts(cancel)[0]
    assert main.scheduled_timers == {}
    env.execute.assert_not_called()


def test_cancel_keeps_other_users_delayed_run(env):
    timer = MagicMock()
    main.scheduled_timers[timer] = OTHER
    update = _update("/cancel")
    asyncio.run(main.cancel(update, None))
    timer.cancel.assert_not_called()
    assert timer in main.scheduled_timers


def test_delayed_run_waits_in_queue_behind_running_job(env, monkeypatch):
    release, calls = _blocking_planner(monkeypatch)
    plan = Plan(steps=[Step(tool="take_screenshot")], message="遅延完了", delay=1)
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
    fired = []
    monkeypatch.setattr(main.scheduler, "schedule", lambda seconds, callback: fired.append(callback))

    async def scenario():
        loop = asyncio.get_running_loop()
        await main.receive(_update("先の処理"), None)
        await _until(lambda: calls == ["先の処理"])
        main._schedule_delayed(plan, message, USER, loop)
        await asyncio.to_thread(fired[0])
        assert main.jobs.running_label(USER) == "先の処理"
        env.execute.assert_not_called()
        release.set()
        await main.jobs.join()

    asyncio.run(scenario())
    assert env.execute.call_args_list[-1].args == (plan,)
    message.reply_text.assert_awaited_once_with("遅延完了")


def test_delayed_failure_is_reported(env, monkeypatch):
    env.execute.side_effect = RuntimeError("boom")
    message = SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(main.run_delayed(Plan(steps=[Step(tool="take_screenshot")]), message))
    assert "boom" in message.reply_text.call_args.args[0]


def _telegram_update(app, text, user_id=USER, command=False):
    entities = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))] if command else []
    message = Message(
        message_id=1, date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, first_name="t", is_bot=False),
        text=text, entities=entities,
    )
    message.set_bot(app.bot)
    return Update(update_id=1, message=message)


def test_cancel_command_is_registered_and_not_sent_to_planner(monkeypatch):
    monkeypatch.setattr(main, "TOKEN", "123:abc")
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    app = main.build_application()
    app.bot._bot_user = User(id=1, first_name="b", is_bot=True, username="main_test_bot")
    handlers = app.handlers[0]
    command = next(h for h in handlers if isinstance(h, CommandHandler))
    text = next(h for h in handlers if isinstance(h, MessageHandler))

    assert "cancel" in command.commands
    cancel_update = _telegram_update(app, "/cancel", command=True)
    assert command.check_update(cancel_update)
    assert not text.check_update(cancel_update)
    assert text.check_update(_telegram_update(app, "こんにちは"))
    assert not command.check_update(_telegram_update(app, "/cancel", user_id=OTHER, command=True))


def test_dev_mode_failure_runs_in_queue_and_is_reported(env, monkeypatch):
    monkeypatch.setattr(main, "open_dev_mode", MagicMock(side_effect=RuntimeError("boom")))
    update = _update("開発モード")
    run_and_drain(main.receive(update, None))
    assert "boom" in _texts(update)[0]
    assert main.app_context.dev_mode is False
