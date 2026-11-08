"""送信者制限・AppleScript入力・外部送信・失敗検知の回帰テスト(OS操作はモックする)。"""
import asyncio
import subprocess
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User

import config
import main
from helpers import run_and_drain
from agent import planner as planner_module
from agent.executor import ToolManager
from agent.models import Intent, Plan, Step, Tool
from agent.planner import Planner
from agent.process import CommandError, run_applescript, run_command
from agent.schemas import NoArgs
from context import Context
from tools.browser.open_bookmark import open_bookmark
from tools.calendar.calendar_add import calendar_add
from tools.mail.mail_send import mail_send

ALLOWED_ID = 111
ATTACK = 'x" & (do shell script "touch /tmp/pwned") & "'


def _update(user_id=ALLOWED_ID, chat_type=Chat.PRIVATE, edited=False):
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, first_name="t", is_bot=False),
        text="hello",
    )
    if edited:
        return Update(update_id=1, edited_message=message)
    return Update(update_id=1, message=message)


@pytest.fixture
def fake_run(monkeypatch):
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", run)
    return run


# ── 1. 送信者制限 ──

@pytest.mark.parametrize("update, accepted", [
    (_update(), True),
    (_update(user_id=222), False),
    (_update(chat_type=Chat.GROUP), False),
    (_update(edited=True), False),
])
def test_filter_accepts_only_allowed_private_messages(update, accepted):
    message_filter = main.build_message_filter({ALLOWED_ID})
    assert bool(message_filter.check_update(update)) is accepted


def test_main_refuses_to_start_without_allowed_ids(monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset())
    build = MagicMock()
    monkeypatch.setattr(main, "ApplicationBuilder", build)
    with pytest.raises(SystemExit):
        main.main()
    build.assert_not_called()


@pytest.mark.parametrize("raw, expected", [
    ("", frozenset()),
    ("123", frozenset({123})),
    (" 123, 456 ,", frozenset({123, 456})),
])
def test_parse_allowed_user_ids(raw, expected):
    assert config.parse_allowed_user_ids(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "0", "1;2"])
def test_parse_allowed_user_ids_rejects_invalid(raw):
    with pytest.raises(ValueError):
        config.parse_allowed_user_ids(raw)


# ── 2. AppleScript入力 ──

def _osascript_call(run):
    args = run.call_args.args[0]
    assert args[:2] == ["osascript", "-e"]
    assert args[3] == "--"
    return args[2], args[4:]


def test_mail_values_stay_out_of_script_source(fake_run):
    mail_send(ATTACK, ATTACK, ATTACK)
    script, values = _osascript_call(fake_run)
    assert "do shell script" not in script
    assert values == [ATTACK, ATTACK, ATTACK]


def test_calendar_title_stays_out_of_script_source(fake_run):
    calendar_add(ATTACK, "2026/08/26", "14:00")
    script, values = _osascript_call(fake_run)
    assert "do shell script" not in script
    assert values == [ATTACK, "2026/08/26 14:00:00"]


@pytest.mark.parametrize("date, time", [
    (ATTACK, "14:00"),
    ("2026/08/26", ATTACK),
    ("2026-08-26", "14:00"),
    ("2026/13/01", "14:00"),
])
def test_calendar_rejects_invalid_datetime_without_running(fake_run, date, time):
    with pytest.raises(ValueError):
        calendar_add("t", date, time)
    fake_run.assert_not_called()


# ── 3. 外部送信 ──

def test_router_has_no_external_model():
    models = [m for m in config.MODEL_ROUTER.values() if m]
    assert models
    assert not any(m.startswith("gpt-") for m in models)


def test_planner_uses_only_local_endpoint():
    assert config.OLLAMA_BASE_URL.startswith("http://localhost:")
    assert not hasattr(Planner, "openai_client")


def _planner(monkeypatch, difficulty, create):
    planner = Planner(Context())
    monkeypatch.setattr(planner, "_classify", lambda *a: difficulty)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    planner._ollama_client = client
    return planner


def test_api_difficulty_does_not_call_any_model(monkeypatch):
    create = MagicMock()
    plan = _planner(monkeypatch, "api", create).plan(Intent("難しい依頼"))
    create.assert_not_called()
    assert plan.steps == []
    assert "外部APIには送信していません" in plan.message


def test_fallback_stops_at_local_models(monkeypatch):
    create = MagicMock(side_effect=RuntimeError("local failure"))
    plan = _planner(monkeypatch, "easy", create).plan(Intent("依頼"))
    assert plan is None
    called = [c.kwargs["model"] for c in create.call_args_list]
    assert called == [
        config.MODEL_ROUTER["easy"],
        config.MODEL_ROUTER["medium"],
        config.MODEL_ROUTER["hard"],
    ]


# ── 4. 失敗検知 ──

def test_run_command_raises_on_nonzero_exit_without_leaking_args(monkeypatch):
    monkeypatch.setattr(subprocess, "run", MagicMock(
        return_value=subprocess.CompletedProcess([], 3, stdout="", stderr="boom")
    ))
    with pytest.raises(CommandError) as e:
        run_command(["tool", "secret-body"])
    assert "boom" in str(e.value) and "3" in str(e.value)
    assert "secret-body" not in str(e.value)


def test_run_command_reports_timeout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", MagicMock(
        side_effect=subprocess.TimeoutExpired("tool", 1)
    ))
    with pytest.raises(CommandError):
        run_command(["tool"], timeout=1)


def test_run_command_reports_missing_executable():
    with pytest.raises(CommandError):
        run_command(["definitely-not-a-real-command"])


def test_run_applescript_passes_values_as_arguments(fake_run):
    run_applescript("script", "-s", "v")
    assert fake_run.call_args.args[0] == ["osascript", "-e", "script", "--", "-s", "v"]


def test_unknown_bookmark_is_an_error(fake_run):
    with pytest.raises(ValueError):
        open_bookmark("存在しない")
    fake_run.assert_not_called()


def _tool_manager(*tools):
    manager = ToolManager.__new__(ToolManager)
    manager.registry = SimpleNamespace(get={t.name: t for t in tools}.get)
    return manager


def test_execute_plan_reports_failed_and_completed_steps():
    def fail():
        raise CommandError("boom")

    manager = _tool_manager(
        Tool(name="ok", function=lambda: "done", description="", args_schema=NoArgs),
        Tool(name="bad", function=fail, description="", args_schema=NoArgs),
    )
    plan = Plan(steps=[Step(tool="ok"), Step(tool="bad"), Step(tool="ok")])
    with pytest.raises(RuntimeError) as e:
        manager.execute_plan(plan)
    assert "2/3" in str(e.value) and "bad" in str(e.value)
    assert "完了済み: ok" in str(e.value)


def _telegram_update():
    return SimpleNamespace(
        message=SimpleNamespace(text="依頼", chat_id=ALLOWED_ID, reply_text=AsyncMock(), reply_photo=AsyncMock()),
        effective_user=SimpleNamespace(id=ALLOWED_ID),
    )


def test_receive_reports_failure_instead_of_llm_success_message(monkeypatch):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="送信しました！")
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    monkeypatch.setattr(main.tm, "execute_plan", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    update = _telegram_update()

    run_and_drain(main.receive(update, None))

    replies = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert "送信しました！" not in replies
    assert any("boom" in r for r in replies)


def test_receive_reports_dev_mode_failure(monkeypatch):
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main, "open_dev_mode", MagicMock(side_effect=CommandError("boom")))
    update = _telegram_update()
    update.message.text = "開発モード"

    run_and_drain(main.receive(update, None))

    assert main.app_context.dev_mode is False
    assert "boom" in update.message.reply_text.call_args.args[0]


@pytest.mark.parametrize("fails", [False, True])
def test_delayed_plan_notifies_real_outcome(monkeypatch, fails):
    plan = Plan(steps=[Step(tool="take_screenshot")], message="完了文面", delay=1)
    monkeypatch.setattr(main.agent, "plan", lambda text: plan)
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    execute = MagicMock(side_effect=RuntimeError("boom")) if fails else MagicMock(return_value=[])
    monkeypatch.setattr(main.tm, "execute_plan", execute)

    threads = []

    def schedule(seconds, callback):
        threads.append(threading.Thread(target=callback))

    monkeypatch.setattr(main.scheduler, "schedule", schedule)
    update = _telegram_update()

    async def scenario():
        await main.receive(update, None)
        await main.jobs.join()
        replies = [c.args[0] for c in update.message.reply_text.call_args_list]
        assert "完了文面" not in replies
        execute.assert_not_called()
        threads[0].start()
        await asyncio.to_thread(threads[0].join)
        await main.jobs.join()

    asyncio.run(scenario())

    last = update.message.reply_text.call_args.args[0]
    assert ("boom" in last) if fails else (last == "完了文面")
