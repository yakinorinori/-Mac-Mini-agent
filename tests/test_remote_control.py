"""Remote Control常駐の設定の回帰テスト(OS操作はモックする)。"""
import json
import plistlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ops import bot, install_dev, system


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "agent" / "state"))
    return tmp_path


@pytest.fixture
def fake_run(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    return run


def test_plist_starts_server_without_inheriting_environment(tmp_path):
    claude = tmp_path / "bin" / "claude"
    data = plistlib.loads(install_dev.render_plist(claude, tmp_path / "dev", tmp_path / "state"))
    assert data["Label"] == "com.agent.remote-control"
    assert data["ProgramArguments"] == [
        str(claude), "remote-control", "--spawn", "worktree", "--capacity", "2", "--permission-mode", "default",
    ]
    assert data["WorkingDirectory"] == str(tmp_path / "dev")
    assert data["KeepAlive"] is True
    assert list(data["EnvironmentVariables"]) == ["PATH"]


def test_settings_protect_production_and_never_bypass():
    deny = install_dev.SETTINGS["permissions"]["deny"]
    assert "Read(~/agent/state/**)" in deny
    assert "Edit(~/agent/prod/**)" in deny
    assert "Edit(~/agent/releases/**)" in deny
    sandbox = install_dev.SETTINGS["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"]
    assert sandbox["allowUnsandboxedCommands"] is False
    assert "~/agent/state" in sandbox["filesystem"]["denyRead"]
    assert "bypassPermissions" not in json.dumps(install_dev.SETTINGS)
    assert "bypassPermissions" not in install_dev.SERVER_ARGUMENTS


def test_settings_merge_keeps_existing_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "model": "keep",
        "permissions": {"deny": ["Read(./x)"], "allow": ["Bash(ls)"]},
        "sandbox": {"network": {"allowedDomains": ["example.com"]}},
    }))
    install_dev.apply_settings(path)
    merged = json.loads(path.read_text())
    assert merged["model"] == "keep"
    assert merged["permissions"]["allow"] == ["Bash(ls)"]
    assert merged["permissions"]["deny"][0] == "Read(./x)"
    assert "Read(~/agent/state/**)" in merged["permissions"]["deny"]
    assert "example.com" in merged["sandbox"]["network"]["allowedDomains"]
    assert "github.com" in merged["sandbox"]["network"]["allowedDomains"]
    assert (tmp_path / "settings.json.before-agent").is_file()


def test_applying_settings_twice_adds_no_duplicates(tmp_path):
    path = tmp_path / "settings.json"
    install_dev.apply_settings(path)
    first = path.read_text()
    install_dev.apply_settings(path)
    assert path.read_text() == first


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_api_key_in_settings_is_refused(tmp_path, name):
    path = tmp_path / "settings.json"
    original = json.dumps({"env": {name: "x"}})
    path.write_text(original)
    with pytest.raises(ValueError):
        install_dev.apply_settings(path)
    assert path.read_text() == original


def test_invalid_settings_are_not_overwritten(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[]")
    with pytest.raises(ValueError):
        install_dev.apply_settings(path)
    assert path.read_text() == "[]"


def test_clone_is_skipped_when_present(tmp_path, fake_run):
    (tmp_path / "dev" / ".git").mkdir(parents=True)
    assert install_dev.ensure_clone(tmp_path / "dev") is False
    fake_run.assert_not_called()


def test_clone_uses_https_remote(tmp_path, fake_run):
    assert install_dev.ensure_clone(tmp_path / "dev") is True
    args = fake_run.call_args.args[0]
    assert args[:2] == ["git", "clone"]
    assert args[2].startswith("https://github.com/")


def test_prepare_needs_claude(home, fake_run, monkeypatch, capsys):
    monkeypatch.setattr(install_dev, "find_claude", lambda: None)
    assert install_dev.prepare(home / "agent") == 1
    fake_run.assert_not_called()
    assert not (home / ".claude" / "settings.json").exists()


def test_prepare_applies_settings_and_clones(home, fake_run, monkeypatch, capsys):
    monkeypatch.setattr(install_dev, "find_claude", lambda: Path("/bin/claude"))
    assert install_dev.prepare(home / "agent") == 0
    assert json.loads((home / ".claude" / "settings.json").read_text())["sandbox"]["enabled"] is True
    assert "remote-control" in capsys.readouterr().out


def test_register_requires_prepare(home, fake_run, monkeypatch):
    monkeypatch.setattr(install_dev, "find_claude", lambda: Path("/bin/claude"))
    assert install_dev.register(home / "agent") == 1
    assert not system.plist_path("remote-control").exists()


def test_register_requires_sandbox_settings(home, fake_run, monkeypatch):
    monkeypatch.setattr(install_dev, "find_claude", lambda: Path("/bin/claude"))
    (home / "agent" / "dev" / ".git").mkdir(parents=True)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}")
    assert install_dev.register(home / "agent") == 1


def test_register_installs_service(home, fake_run, monkeypatch):
    monkeypatch.setattr(install_dev, "find_claude", lambda: Path("/bin/claude"))
    monkeypatch.setattr(install_dev, "wait_until_unloaded", MagicMock())
    bootstrap = MagicMock()
    monkeypatch.setattr(install_dev, "bootstrap_service", bootstrap)
    (home / "agent" / "dev" / ".git").mkdir(parents=True)
    install_dev.apply_settings(home / ".claude" / "settings.json")
    assert install_dev.register(home / "agent") == 0
    bootstrap.assert_called_once_with(system.plist_path("remote-control"))


def _healthy(monkeypatch, remote_running):
    def state(target):
        running = remote_running if target == "remote-control" else True
        return {"loaded": running, "running": running, "pid": 1}

    monkeypatch.setattr(system, "service_state", state)
    monkeypatch.setattr(system, "memory_free_percent", lambda: 50)
    monkeypatch.setattr(system, "disk_free_gb", lambda: 100.0)
    monkeypatch.setattr(system, "read_heartbeat", lambda name: {"time": 9e12, "commit": "abc"})


def test_stopped_remote_control_alerts_only_when_installed(home, monkeypatch):
    _healthy(monkeypatch, remote_running=False)
    assert bot.check_health() == set()
    path = system.plist_path("remote-control")
    path.parent.mkdir(parents=True)
    path.touch()
    assert "Remote Controlが停止しています" in bot.check_health()


def test_status_shows_remote_control(home, monkeypatch):
    _healthy(monkeypatch, remote_running=True)
    monkeypatch.setattr(system, "swap_usage", lambda: None)
    monkeypatch.setattr(system, "github_main_status", lambda: None)
    assert "remote-control: 稼働中" in bot.collect_status()
