"""Ops Bot、heartbeat、launchd設定、ログ内容の回帰テスト(OS操作はモックする)。"""
import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

import heartbeat
import main
from helpers import run_and_drain
from agent.models import Intent, Plan, Step
from agent.planner import Planner
from approvals import ApprovalStore
from context import Context
from ops import bot, install, system

USER = 111
OTHER = 222
REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "本文の秘密"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_run(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    return run


# ── ログに本文を残さない ──

def test_received_text_is_not_logged(monkeypatch, capsys):
    monkeypatch.setattr(main.agent, "plan", lambda text: None)
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    update = SimpleNamespace(
        message=SimpleNamespace(text=SECRET, reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=USER),
    )
    run_and_drain(main.receive(update, None))
    assert SECRET not in capsys.readouterr().out


def test_llm_response_is_not_logged(capsys):
    args = {"to": "a@example.com", "subject": SECRET, "body": SECRET}
    content = json.dumps(
        {"steps": [{"tool": "mail_send", "args": args}], "message": SECRET},
        ensure_ascii=False,
    )
    create = MagicMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    ))
    planner = Planner(Context(), registry=main.tm.registry)
    planner._classify = lambda *a: "easy"
    planner._ollama_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    plan = planner.plan(Intent("依頼"))

    output = capsys.readouterr().out
    assert plan.steps[0].tool == "mail_send"
    assert SECRET not in output
    assert "mail_send" in output


def test_ops_bot_does_not_load_llm_or_tools():
    code = (
        "import sys, ops.bot; "
        "print(any(m in sys.modules for m in ['main', 'openai', 'agent.planner', 'tools.mail.mail_send']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.stdout.strip() == "False"


# ── heartbeat ──

def test_heartbeat_is_written_and_readable(state):
    heartbeat.write_heartbeat(state, "prod", "abc1234", now=100.0)
    data = system.read_heartbeat("prod")
    assert data["commit"] == "abc1234" and data["time"] == 100.0
    assert [p.name for p in (state / "heartbeat").iterdir()] == ["prod.json"]


@pytest.mark.parametrize("content", ["not json", "[]", '{"time": "x"}'])
def test_corrupt_heartbeat_is_ignored(state, content):
    (state / "heartbeat").mkdir()
    (state / "heartbeat" / "prod.json").write_text(content)
    assert system.read_heartbeat("prod") is None


def test_bot_writes_heartbeat_when_state_dir_is_set(state, monkeypatch):
    monkeypatch.setattr(main, "TOKEN", "123:abc")

    async def scenario():
        app = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        await main._on_start(app)
        await asyncio.sleep(0.05)
        await main._on_stop(app)

    asyncio.run(scenario())
    assert system.read_heartbeat("prod") is not None


def test_bot_skips_heartbeat_without_state_dir(monkeypatch):
    monkeypatch.delenv("AGENT_STATE_DIR", raising=False)
    app = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(main._on_start(app))
    assert app.bot_data == {}


# ── launchctl ──

RUNNING = "gui/501/com.agent.prod = {\n\tstate = running\n\tpid = 4321\n}"


def test_service_state_parses_running(fake_run):
    fake_run.return_value = subprocess.CompletedProcess([], 0, stdout=RUNNING, stderr="")
    assert system.service_state("prod") == {"loaded": True, "running": True, "pid": 4321}


def test_service_state_handles_unknown_service(fake_run):
    fake_run.return_value = subprocess.CompletedProcess([], 113, stdout="", stderr="Could not find")
    assert system.service_state("prod") == {"loaded": False, "running": False, "pid": None}


def test_tests_do_not_see_the_real_home():
    assert not system.plist_path("remote-control").exists()
    assert str(Path.home()).startswith(os.environ["HOME"])


def test_wait_until_unloaded_polls_until_gone(monkeypatch):
    states = iter([True, True, False])
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": next(states)})
    sleep = MagicMock()
    monkeypatch.setattr(system.time, "sleep", sleep)
    system.wait_until_unloaded("prod")
    assert sleep.call_count == 2


def test_wait_until_unloaded_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": True})
    monkeypatch.setattr(system.time, "sleep", MagicMock())
    system.wait_until_unloaded("prod", timeout=0)


def test_stop_service_waits_for_unload(monkeypatch, fake_run):
    wait = MagicMock()
    monkeypatch.setattr(system, "wait_until_unloaded", wait)
    system.stop_service("prod")
    wait.assert_called_once_with("prod")


def test_bootstrap_retries_transient_failure(fake_run, monkeypatch, tmp_path):
    monkeypatch.setattr(system.time, "sleep", MagicMock())
    fake_run.side_effect = [
        subprocess.CompletedProcess([], 5, stdout="", stderr="Input/output error"),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    ]
    system.bootstrap_service(tmp_path / "com.agent.prod.plist")
    assert fake_run.call_count == 2


def test_bootstrap_reports_which_service_failed(fake_run, monkeypatch, tmp_path):
    monkeypatch.setattr(system.time, "sleep", MagicMock())
    fake_run.return_value = subprocess.CompletedProcess([], 5, stdout="", stderr="Input/output error")
    with pytest.raises(system.CommandError) as e:
        system.bootstrap_service(tmp_path / "com.agent.prod.plist")
    assert "com.agent.prod.plist" in str(e.value)
    assert fake_run.call_count == system.BOOTSTRAP_ATTEMPTS


def test_memory_percent_is_parsed(fake_run):
    fake_run.return_value = subprocess.CompletedProcess(
        [], 0, stdout="System-wide memory free percentage: 37%\n", stderr=""
    )
    assert system.memory_free_percent() == 37


def test_memory_percent_tolerates_failure(fake_run):
    fake_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="x")
    assert system.memory_free_percent() is None


# ── ログの伏せ字とローテーション ──

@pytest.mark.parametrize("secret", [
    "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "sk-abcdefghijklmnopqrstuvwxyz",
    "ghp_abcdefghijklmnopqrstuvwxyz123456",
    "AKIAABCDEFGHIJKLMNOP",
    "Authorization: Bearer abcdefghijkl",
    "TELEGRAM_BOT_TOKEN=abcdefg",
    "password: hunter2",
    "user@example.com",
])
def test_secrets_are_redacted(secret):
    value = secret.split("=")[-1].split(": ")[-1].split("Bearer ")[-1]
    assert value not in system.redact(f"前 {secret} 後")


def test_tail_returns_last_lines_redacted(state):
    (state / "logs").mkdir()
    lines = [f"行{i}" for i in range(100)] + ["連絡先 user@example.com"]
    (state / "logs" / "prod.log").write_text("\n".join(lines), encoding="utf-8")
    result = system.tail_log("prod", lines=3).splitlines()
    assert result[:2] == ["行98", "行99"]
    assert "user@example.com" not in result[2]


def test_tail_of_missing_log_is_empty(state):
    assert system.tail_log("prod") == ""


def test_rotation_truncates_in_place(tmp_path):
    log = tmp_path / "prod.log"
    log.write_bytes(b"x" * 11)
    with log.open("ab") as writer:
        system.rotate_log(log, max_bytes=10)
        writer.write(b"y")
        writer.flush()
    assert log.read_bytes() == b"y"
    assert (tmp_path / "prod.log.1").read_bytes() == b"x" * 11


def test_small_log_is_not_rotated(tmp_path):
    log = tmp_path / "prod.log"
    log.write_bytes(b"x")
    system.rotate_log(log, max_bytes=10)
    assert log.read_bytes() == b"x"
    assert not (tmp_path / "prod.log.1").exists()


# ── 監視 ──

@pytest.fixture
def healthy(state, monkeypatch):
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": True, "running": True, "pid": 1})
    monkeypatch.setattr(system, "memory_free_percent", lambda: 50)
    monkeypatch.setattr(system, "disk_free_gb", lambda: 100.0)
    heartbeat.write_heartbeat(state, "prod", "abc")
    return state


def test_healthy_system_has_no_problems(healthy):
    assert bot.check_health() == set()


def test_stopped_prod_is_a_problem(healthy, monkeypatch):
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": True, "running": False, "pid": None})
    assert "本番Botが停止しています" in bot.check_health()


def test_intentional_stop_is_not_a_problem(healthy, monkeypatch):
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": False, "running": False, "pid": None})
    system.stop_marker("prod").touch()
    assert bot.check_health() == set()


def test_stale_heartbeat_is_a_problem(healthy):
    heartbeat.write_heartbeat(healthy, "prod", "abc", now=time.time() - bot.HEARTBEAT_STALE_SECONDS - 1)
    assert "本番Botの応答記録が更新されていません" in bot.check_health()


def test_low_resources_are_problems(healthy, monkeypatch):
    monkeypatch.setattr(system, "memory_free_percent", lambda: bot.MEMORY_FREE_ALERT_PERCENT - 1)
    monkeypatch.setattr(system, "disk_free_gb", lambda: bot.DISK_FREE_ALERT_GB - 1)
    assert len(bot.check_health()) == 2


def test_alerts_only_on_change():
    assert bot.alert_messages(set(), {"A"}) == ["⚠️ A"]
    assert bot.alert_messages({"A"}, {"A"}) == []
    assert bot.alert_messages({"A"}, set()) == ["✅ 解消しました: A"]


# ── コマンド ──

def _message():
    return SimpleNamespace(text="", reply_text=AsyncMock())


def _update(user_id=USER):
    return SimpleNamespace(message=_message(), effective_user=SimpleNamespace(id=user_id))


def _context(*args):
    return SimpleNamespace(args=list(args))


@pytest.fixture
def ops_env(state, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    monkeypatch.setattr(bot, "approvals", ApprovalStore())
    actions = SimpleNamespace(
        stop=MagicMock(), start=MagicMock(), restart=MagicMock(),
    )
    monkeypatch.setattr(system, "stop_service", actions.stop)
    monkeypatch.setattr(system, "start_service", actions.start)
    monkeypatch.setattr(system, "restart_service", actions.restart)
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": False, "running": False, "pid": None})
    return actions


def _buttons(update):
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    approve, reject = markup.inline_keyboard[0]
    return approve.callback_data, reject.callback_data


def _press(data, user_id=USER):
    query = SimpleNamespace(
        data=data, message=_message(), answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
    asyncio.run(bot.on_decision(update, None))
    return query


def test_stop_requires_approval_then_marks_and_stops(ops_env):
    update = _update()
    asyncio.run(bot.request_stop(update, _context()))
    ops_env.stop.assert_not_called()

    approve, _ = _buttons(update)
    _press(approve)

    ops_env.stop.assert_called_once_with("prod")
    assert system.stop_marker("prod").exists()


def test_failed_stop_clears_marker_when_still_running(ops_env, monkeypatch):
    ops_env.stop.side_effect = RuntimeError("boom")
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": True, "running": True, "pid": 1})
    update = _update()
    asyncio.run(bot.request_stop(update, _context()))
    approve, _ = _buttons(update)
    query = _press(approve)
    assert not system.stop_marker("prod").exists()
    assert "boom" in query.message.reply_text.call_args.args[0]


def test_start_clears_marker(ops_env):
    system.stop_marker("prod").touch()
    update = _update()
    asyncio.run(bot.request_start(update, _context()))
    approve, _ = _buttons(update)
    _press(approve)
    ops_env.start.assert_called_once_with("prod")
    assert not system.stop_marker("prod").exists()


def test_failed_start_keeps_marker(ops_env):
    system.stop_marker("prod").touch()
    ops_env.start.side_effect = RuntimeError("boom")
    update = _update()
    asyncio.run(bot.request_start(update, _context()))
    approve, _ = _buttons(update)
    _press(approve)
    assert system.stop_marker("prod").exists()


def test_reject_does_nothing(ops_env):
    update = _update()
    asyncio.run(bot.request_stop(update, _context()))
    _, reject = _buttons(update)
    query = _press(reject)
    ops_env.stop.assert_not_called()
    assert "中止しました" in query.message.reply_text.call_args.args[0]


def test_double_press_runs_once(ops_env):
    update = _update()
    asyncio.run(bot.request_stop(update, _context()))
    approve, _ = _buttons(update)
    _press(approve)
    _press(approve)
    ops_env.stop.assert_called_once()


def test_other_user_cannot_approve(ops_env):
    update = _update()
    asyncio.run(bot.request_stop(update, _context()))
    approve, _ = _buttons(update)
    query = _press(approve, user_id=OTHER)
    ops_env.stop.assert_not_called()
    query.message.reply_text.assert_not_called()


@pytest.mark.parametrize("target", ["prod", "ops"])
def test_restart_valid_target(ops_env, target):
    update = _update()
    asyncio.run(bot.request_restart(update, _context(target)))
    approve, _ = _buttons(update)
    _press(approve)
    ops_env.restart.assert_called_once_with(target)


def test_ops_restart_replies_before_restarting(ops_env):
    order = []
    ops_env.restart.side_effect = lambda target: order.append("restart")
    message = _message()
    message.reply_text = AsyncMock(side_effect=lambda text: order.append("reply"))
    asyncio.run(bot.run_action(message, ("restart", "ops")))
    assert order == ["reply", "restart"]


@pytest.mark.parametrize("args", [[], ["../etc"], ["prod", "ops"], ["com.apple.Finder"]])
def test_restart_and_logs_reject_invalid_targets(ops_env, args):
    update = _update()
    asyncio.run(bot.request_restart(update, _context(*args)))
    assert "使い方" in update.message.reply_text.call_args.args[0]
    update = _update()
    asyncio.run(bot.show_logs(update, _context(*args)))
    assert "使い方" in update.message.reply_text.call_args.args[0]


def test_logs_are_limited_and_redacted(ops_env, state):
    (state / "logs").mkdir()
    (state / "logs" / "prod.log").write_text(
        "user@example.com\n" + "\n".join("あ" * 200 for _ in range(49)), encoding="utf-8"
    )
    update = _update()
    asyncio.run(bot.show_logs(update, _context("prod")))
    text = update.message.reply_text.call_args.args[0]
    assert len(text) <= bot.MAX_MESSAGE_LENGTH
    assert "user@example.com" not in text


def test_status_works_when_everything_is_unavailable(ops_env, state, monkeypatch):
    monkeypatch.setattr(system, "memory_free_percent", lambda: None)
    monkeypatch.setattr(system, "swap_usage", lambda: None)
    monkeypatch.setattr(system, "github_main_status", lambda: None)
    text = bot.collect_status()
    assert "prod: 未登録" in text
    assert "メモリ空き: 取得できません" in text
    assert "GitHub main: 取得できません" in text


def test_every_command_requires_allowed_private_user(monkeypatch):
    monkeypatch.setattr(bot, "TOKEN", "123:abc")
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    app = bot.build_application()
    from telegram import User as TelegramUser
    app.bot._bot_user = TelegramUser(id=1, first_name="ops", is_bot=True, username="ops_test_bot")
    commands = [h for h in app.handlers[0] if isinstance(h, CommandHandler)]
    names = {name for h in commands for name in h.commands}
    assert names == {
        "start", "help", "status", "logs", "stop_prod", "start_prod", "restart",
        "deploy", "rollback", "deploy_staging", "stop_staging",
    }

    def update_for(user_id, chat_type):
        from datetime import datetime, timezone
        from telegram import Chat, Message, MessageEntity, Update, User
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type=chat_type),
            from_user=User(id=user_id, first_name="t", is_bot=False),
            text="/status",
            entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=7)],
        )
        message.set_bot(app.bot)
        return Update(update_id=1, message=message)

    status = next(h for h in commands if "status" in h.commands)
    assert status.check_update(update_for(USER, "private"))
    assert not status.check_update(update_for(OTHER, "private"))
    assert not status.check_update(update_for(USER, "group"))
    assert any(isinstance(h, CallbackQueryHandler) for h in app.handlers[0])


def test_ops_refuses_to_start_without_allowed_users(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_IDS", frozenset())
    with pytest.raises(SystemExit):
        bot.main()


# ── launchd設定 ──

@pytest.mark.parametrize("target, directory, arguments", [
    ("prod", "agent/prod", ["main.py"]),
    ("staging", "agent/staging", ["main.py"]),
    ("ops", "個人事業/repo", ["-m", "ops.bot"]),
    ("runner", "個人事業/repo", ["-m", "ops.runner"]),
])
def test_plist_contents(target, directory, arguments, tmp_path):
    repo = tmp_path / "個人事業" / "repo"
    root = tmp_path / "agent"
    data = plistlib.loads(install.render_plist(target, repo, root))
    code = tmp_path / directory
    assert data["Label"] == f"com.agent.{target}"
    assert data["ProgramArguments"] == [str(code / ".venv/bin/python"), *arguments]
    assert data["WorkingDirectory"] == str(code)
    assert data["LimitLoadToSessionType"] == "Aqua"
    assert data["EnvironmentVariables"]["AGENT_NAME"] == target
    assert data["StandardOutPath"] == str(root / "state" / "logs" / f"{target}.log")


def test_service_keepalive_settings(tmp_path):
    render = lambda target: plistlib.loads(install.render_plist(target, tmp_path, tmp_path))
    assert render("prod")["KeepAlive"] is True
    assert render("ops")["KeepAlive"] is True
    assert "KeepAlive" not in render("staging")
    runner = render("runner")
    assert "KeepAlive" not in runner and "RunAtLoad" not in runner
    assert runner["QueueDirectories"] == [str(tmp_path / "state" / "requests")]
    assert runner["EnvironmentVariables"]["AGENT_ENV_FILE"] == str(tmp_path / "state" / "ops.env")


def test_plist_contains_no_secret_values(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "prod.env").write_text("TELEGRAM_BOT_TOKEN=secret-value")
    assert b"secret-value" not in install.render_plist("prod", tmp_path, tmp_path)


def _prepared(tmp_path, mode=0o600):
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").touch()
    root = tmp_path / "agent"
    (root / "state").mkdir(parents=True)
    for target in ("prod", "ops"):
        env = root / "state" / f"{target}.env"
        env.touch()
        env.chmod(mode)
    return repo, root


def test_install_stops_on_missing_env_file(tmp_path, fake_run, capsys):
    repo, root = _prepared(tmp_path)
    (root / "state" / "ops.env").unlink()
    assert install.install(repo, root) == 1
    fake_run.assert_not_called()
    assert "ops.env" in capsys.readouterr().err


def test_install_stops_on_open_env_permissions(tmp_path, fake_run):
    repo, root = _prepared(tmp_path, mode=0o644)
    assert install.install(repo, root) == 1
    fake_run.assert_not_called()


def test_install_registers_services_and_keeps_staging_off(tmp_path, fake_run, monkeypatch):
    repo, root = _prepared(tmp_path)
    monkeypatch.setattr(install, "wait_until_unloaded", MagicMock())
    (root / "releases" / "a").mkdir(parents=True)
    (root / "prod").symlink_to(root / "releases" / "a")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("AGENT_STATE_DIR", str(root / "state"))
    assert install.install(repo, root) == 0
    commands = [c.args[0] for c in fake_run.call_args_list]
    bootstraps = [c for c in commands if c[:2] == ["launchctl", "bootstrap"]]
    assert len(bootstraps) == 3
    agents = tmp_path / "home/Library/LaunchAgents"
    assert sorted(p.name for p in agents.iterdir()) == [
        "com.agent.ops.plist", "com.agent.prod.plist", "com.agent.runner.plist",
    ]
    assert (root / "state" / "com.agent.staging.plist").is_file()
    assert (root / "state" / "requests").stat().st_mode & 0o077 == 0


def test_install_creates_first_release_when_prod_is_missing(tmp_path, fake_run, monkeypatch):
    repo, root = _prepared(tmp_path)
    monkeypatch.setattr(install, "wait_until_unloaded", MagicMock())
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("AGENT_STATE_DIR", str(root / "state"))
    initialize = MagicMock(return_value="a" * 40)
    monkeypatch.setattr(install.Deployer, "initialize", initialize)
    assert install.install(repo, root, ref="feat/x") == 0
    initialize.assert_called_once_with("feat/x")


def test_reinstall_waits_for_unload_before_registering(tmp_path, fake_run, monkeypatch):
    repo, root = _prepared(tmp_path)
    (root / "releases" / "a").mkdir(parents=True)
    (root / "prod").symlink_to(root / "releases" / "a")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("AGENT_STATE_DIR", str(root / "state"))
    order = []
    monkeypatch.setattr(install, "wait_until_unloaded", lambda t: order.append(("wait", t)))
    monkeypatch.setattr(install, "bootstrap_service", lambda p: order.append(("bootstrap", p.stem)))
    assert install.install(repo, root) == 0
    assert order[:2] == [("wait", "prod"), ("bootstrap", "com.agent.prod")]


def test_dry_run_changes_nothing(tmp_path, fake_run, monkeypatch, capsys):
    repo, root = _prepared(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert install.install(repo, root, dry_run=True) == 0
    fake_run.assert_not_called()
    assert not (tmp_path / "home").exists()
    assert not (root / "releases").exists()
    assert "com.agent.prod" in capsys.readouterr().out


def test_custom_env_file_is_loaded(tmp_path):
    shutil.copy(REPO_ROOT / "config.py", tmp_path / "config.py")
    env_file = tmp_path / "custom.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from-custom\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    env.update(AGENT_ENV_FILE=str(env_file), ALLOWED_TELEGRAM_USER_IDS="")
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.TOKEN)"],
        capture_output=True, text=True, check=True, cwd=tmp_path, env=env,
    )
    assert result.stdout.strip() == "from-custom"
