"""環境設定とツールの入力制限の回帰テスト(OS操作はモックする)。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import APIConnectionError, NotFoundError

import config
from agent.models import Intent
from agent.planner import Planner
from context import Context
from plugins.plugin_manager import PluginManager
from registry.tool_registry import ToolRegistry
from tools.apps.close_app import _QUIT_SCRIPT, close_app
from tools.browser.open_url import open_url
from tools.calendar.calendar_add import calendar_add
from tools.excel.excel_open import excel_open
from tools.screen.take_screenshot import take_screenshot

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_run(monkeypatch):
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", run)
    return run


def _read_token_from_copy(tmp_path, env_file, extra_env):
    shutil.copy(REPO_ROOT / "config.py", tmp_path / "config.py")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    env.update(extra_env, ALLOWED_TELEGRAM_USER_IDS="")
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    return subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import config; print(config.TOKEN)"],
        capture_output=True, text=True, check=True, cwd=other_dir, env=env,
    ).stdout.strip()


def test_env_file_is_loaded_regardless_of_cwd(tmp_path):
    assert _read_token_from_copy(tmp_path, "TELEGRAM_BOT_TOKEN=from-file\n", {}) == "from-file"


def test_existing_environment_takes_priority_over_env_file(tmp_path):
    token = _read_token_from_copy(
        tmp_path, "TELEGRAM_BOT_TOKEN=from-file\n", {"TELEGRAM_BOT_TOKEN": "from-env"}
    )
    assert token == "from-env"


def test_env_files_stay_out_of_git():
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and "!.env.example" in ignored


def test_plugins_load_from_any_cwd_and_ignore_pycache(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (REPO_ROOT / "tools" / "__pycache__").mkdir(exist_ok=True)
    registry = ToolRegistry()

    PluginManager(registry).load_plugins()

    output = capsys.readouterr().out
    assert registry.get("mail_send") is not None
    assert "__pycache__" not in output
    assert "読み込み失敗" not in output


def test_plugin_import_error_is_reported_with_its_cause(capsys):
    registry = ToolRegistry()
    manager = PluginManager(registry, tools_dir=REPO_ROOT / "tools")
    manager.tools_dir = _FakeToolsDir()

    manager.load_plugins()

    assert "読み込み失敗" in capsys.readouterr().out


class _FakePlugin:
    name = "missing_plugin_for_test"

    def __truediv__(self, other):
        return self

    def is_file(self):
        return True

    def __lt__(self, other):
        return False


class _FakeToolsDir:
    def iterdir(self):
        return [_FakePlugin()]


_REQUEST = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
_VALID_PLAN = '{"steps": [{"tool": "calendar_add", "args": {}}], "message": "ok", "delay": null}'


def _not_found():
    return NotFoundError("missing", response=httpx.Response(404, request=_REQUEST), body=None)


def _planner(monkeypatch, difficulty, create):
    planner = Planner(Context())
    monkeypatch.setattr(planner, "_classify", lambda *a: difficulty)
    planner._ollama_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return planner


def _reply(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_hard_request_falls_back_to_lower_local_model(monkeypatch):
    def create(model, **kwargs):
        if model == config.MODEL_ROUTER["hard"]:
            raise _not_found()
        return _reply(_VALID_PLAN)

    mock = MagicMock(side_effect=create)
    plan = _planner(monkeypatch, "hard", mock).plan(Intent("予定を追加して"))

    assert [c.kwargs["model"] for c in mock.call_args_list] == [
        config.MODEL_ROUTER["hard"],
        config.MODEL_ROUTER["medium"],
    ]
    assert plan.steps[0].tool == "calendar_add"


def test_medium_request_tries_higher_then_lower_local_models(monkeypatch):
    mock = MagicMock(side_effect=_not_found())
    _planner(monkeypatch, "medium", mock).plan(Intent("依頼"))
    assert [c.kwargs["model"] for c in mock.call_args_list] == [
        config.MODEL_ROUTER["medium"],
        config.MODEL_ROUTER["hard"],
        config.MODEL_ROUTER["easy"],
    ]


@pytest.mark.parametrize("error, expected", [
    (APIConnectionError(request=_REQUEST), "接続できません"),
    (_not_found(), "モデルが見つかりません"),
])
def test_known_llm_failures_explain_the_cause(monkeypatch, error, expected):
    plan = _planner(monkeypatch, "easy", MagicMock(side_effect=error)).plan(Intent("依頼"))
    assert plan.steps == []
    assert expected in plan.message


def test_calendar_event_sets_end_date(fake_run):
    calendar_add("会議", "2026/11/10", "14:00")
    script = fake_run.call_args.args[0][2]
    assert "set endDate to startDate + (1 * hours)" in script
    assert "end date:endDate" in script


def test_close_app_requests_normal_quit_with_name_as_argument(fake_run):
    close_app(".")
    args = fake_run.call_args.args[0]
    assert args[0] == "osascript" and args[-1] == "."
    assert "pkill" not in " ".join(args)
    assert "to quit" in _QUIT_SCRIPT


@pytest.mark.parametrize("url", ["https://example.com/a?b=1", "HTTP://example.com"])
def test_open_url_accepts_web_urls(fake_run, url):
    open_url(url)
    assert fake_run.call_args.args[0] == ["open", url]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "/Applications/Calculator.app",
    "-a",
    "javascript:alert(1)",
    "ssh://example.com",
    "https://",
])
def test_open_url_rejects_other_targets(fake_run, url):
    with pytest.raises(ValueError):
        open_url(url)
    fake_run.assert_not_called()


def test_excel_open_expands_home_and_opens_file(fake_run, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    workbook = tmp_path / "report.xlsx"
    workbook.touch()
    excel_open("~/report.xlsx")
    assert fake_run.call_args.args[0] == ["open", str(workbook.resolve())]


@pytest.mark.parametrize("name", ["run.command", "Evil.app", "script.sh", "noext"])
def test_excel_open_rejects_other_file_types(fake_run, tmp_path, name):
    target = tmp_path / name
    target.touch()
    with pytest.raises(ValueError):
        excel_open(str(target))
    fake_run.assert_not_called()


def test_excel_open_rejects_symlink_to_other_file_type(fake_run, tmp_path):
    real = tmp_path / "run.command"
    real.touch()
    link = tmp_path / "report.xlsx"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        excel_open(str(link))
    fake_run.assert_not_called()


def test_excel_open_reports_missing_file(fake_run, tmp_path):
    with pytest.raises(FileNotFoundError):
        excel_open(str(tmp_path / "none.xlsx"))
    fake_run.assert_not_called()


def test_screenshot_path_does_not_depend_on_cwd(fake_run, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def create_file(args, **kwargs):
        Path(args[-1]).touch()
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    fake_run.side_effect = create_file
    result = Path(take_screenshot())
    try:
        assert result.is_absolute()
        assert result.parent == REPO_ROOT / "tools" / "screen" / "captures"
    finally:
        result.unlink()
