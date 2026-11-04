"""反映、切り戻し、runner、Ops Botの反映コマンドの回帰テスト。

gitは実物を使い、launchd、venv、pip、テスト実行、GitHub APIは置き換える。
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.process import CommandError
from approvals import ApprovalStore
from ops import bot, runner, system
from ops.deploy import Deployer, DeployError, Paths, is_valid_branch, is_valid_sha, link_target

USER = 111


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class Upstream:
    def __init__(self, path):
        self.path = path
        path.mkdir()
        _git(path, "init", "-q", "-b", "main")

    def commit(self, name):
        (self.path / "file.txt").write_text(name)
        _git(self.path, "add", ".")
        _git(self.path, "commit", "-q", "-m", name)
        return _git(self.path, "rev-parse", "HEAD")

    def branch(self, name):
        _git(self.path, "switch", "-q", "-c", name)

    def main(self):
        _git(self.path, "switch", "-q", "main")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeServices:
    """launchdの代わり。再起動すると、設定された動きでheartbeatを出す。"""

    def __init__(self, state, clock, paths):
        self.state, self.clock, self.paths = state, clock, paths
        self.calls = []
        self.unhealthy = set()
        self.crash_loop = set()
        self.beats = {}
        self.pid = 100
        self.restart_error = None

    def stop_marker(self, target):
        return self.state / f"{target}.stopped"

    def read_heartbeat(self, name):
        return self.beats.get(name)

    def _running_commit(self, name):
        return link_target(self.paths.prod if name == "prod" else self.paths.staging)

    def restart_service(self, target):
        self.calls.append(("restart", target))
        if self.restart_error:
            raise self.restart_error
        self.beats.pop(target, None)

    def start_staging_service(self):
        self.calls.append(("start_staging",))

    def stop_staging_service(self):
        self.calls.append(("stop_staging",))
        self.beats.pop("staging", None)

    def advance(self, seconds):
        self.clock.now += seconds
        for name in ("prod", "staging"):
            commit = self._running_commit(name)
            if not commit or commit in self.unhealthy:
                continue
            if name == "staging" and ("start_staging",) not in self.calls:
                continue
            if name == "prod" and ("restart", "prod") not in self.calls:
                continue
            if commit in self.crash_loop:
                self.pid += 1
            self.beats[name] = {"commit": commit[:7], "time": self.clock.now, "pid": self.pid}


@pytest.fixture
def env(tmp_path, monkeypatch):
    upstream = Upstream(tmp_path / "upstream")
    first = upstream.commit("first")
    root = tmp_path / "agent"
    paths = Paths(root)
    paths.state.mkdir(parents=True)
    paths.requests.mkdir()
    clock = FakeClock()
    services = FakeServices(paths.state, clock, paths)
    notes = []
    ci = {}

    class TestDeployer(Deployer):
        failures = set()

        def _prepare_release(self, path):
            if path.name in self.failures:
                raise CommandError("pytest が失敗しました: boom")

    deployer = TestDeployer(
        paths,
        notify=notes.append,
        ci_conclusion=lambda sha: ci.get(sha, "success"),
        services=services,
        clock=clock,
        sleep=services.advance,
        remote=str(upstream.path),
    )
    deployer.initialize("main")
    return SimpleNamespace(
        upstream=upstream, first=first, paths=paths, services=services,
        notes=notes, ci=ci, deployer=deployer, clock=clock,
    )


def _history(env):
    return [json.loads(line)["event"] for line in env.paths.history.read_text().splitlines()]


def test_initialize_points_prod_to_main(env):
    assert link_target(env.paths.prod) == env.first
    assert (env.paths.releases / env.first / "file.txt").read_text() == "first"
    assert _git(env.paths.releases / env.first, "rev-parse", "HEAD") == env.first


def test_initialize_requires_ci(tmp_path):
    upstream = Upstream(tmp_path / "up")
    upstream.commit("a")
    deployer = Deployer(
        Paths(tmp_path / "agent"), ci_conclusion=lambda sha: "failure", remote=str(upstream.path),
    )
    with pytest.raises(DeployError):
        deployer.initialize("main")


def test_deploy_success_switches_and_keeps_previous(env):
    second = env.upstream.commit("second")
    env.deployer.deploy(second)
    assert link_target(env.paths.prod) == second
    assert link_target(env.paths.previous) == env.first
    assert not env.paths.journal.exists()
    assert ("restart", "prod") in env.services.calls
    assert "deploy_succeeded" in _history(env)
    assert any("✅" in note for note in env.notes)


def test_unhealthy_deploy_reverts_automatically(env):
    env.deployer.deploy(env.upstream.commit("second"))
    second = link_target(env.paths.prod)
    third = env.upstream.commit("third")
    env.services.unhealthy.add(third)
    env.deployer.deploy(third)
    assert link_target(env.paths.prod) == second
    assert not env.paths.journal.exists()
    assert "deploy_reverted" in _history(env)
    assert any("↩️" in note for note in env.notes)
    assert link_target(env.paths.previous) == env.first


def test_rollback_still_works_after_failed_deploy(env):
    second = env.upstream.commit("second")
    env.deployer.deploy(second)
    third = env.upstream.commit("third")
    env.services.unhealthy.add(third)
    env.deployer.deploy(third)
    env.deployer.rollback()
    assert link_target(env.paths.prod) == env.first


def test_failed_first_deploy_leaves_no_rollback_target(env):
    second = env.upstream.commit("second")
    env.services.unhealthy.add(second)
    env.deployer.deploy(second)
    assert link_target(env.paths.prod) == env.first
    assert link_target(env.paths.previous) is None


def test_crash_loop_is_not_treated_as_healthy(env):
    second = env.upstream.commit("second")
    env.services.crash_loop.add(second)
    env.deployer.deploy(second)
    assert link_target(env.paths.prod) == env.first
    assert "deploy_unhealthy" in _history(env)


def test_old_heartbeat_does_not_count(env):
    second = env.upstream.commit("second")
    env.services.unhealthy.add(second)
    env.services.beats["prod"] = {"commit": second[:7], "time": 1.0, "pid": 1}
    assert env.deployer.wait_healthy("prod", second, since=env.clock()) is False


def test_heartbeats_from_before_restart_do_not_count(env):
    sha = "c" * 40
    beats = iter([
        {"commit": sha[:7], "time": env.clock() - 20, "pid": 5},
        {"commit": sha[:7], "time": env.clock() - 10, "pid": 5},
    ])
    env.services.read_heartbeat = lambda name: next(beats, None)
    assert env.deployer.wait_healthy("prod", sha, since=env.clock()) is False


def test_restart_command_failure_reverts(env):
    second = env.upstream.commit("second")
    env.services.restart_error = CommandError("launchctl failed")
    env.deployer.deploy(second)
    assert link_target(env.paths.prod) == env.first
    assert any("🚨" in note for note in env.notes)


def test_failed_tests_leave_prod_untouched(env):
    second = env.upstream.commit("second")
    env.deployer.failures = {second}
    with pytest.raises(DeployError) as e:
        env.deployer.deploy(second)
    assert "本番は変更していません" in str(e.value)
    assert link_target(env.paths.prod) == env.first
    assert ("restart", "prod") not in env.services.calls
    assert not (env.paths.releases / second).exists()


def test_failed_ci_leaves_prod_untouched(env):
    second = env.upstream.commit("second")
    env.ci[second] = "failure"
    with pytest.raises(DeployError):
        env.deployer.deploy(second)
    assert link_target(env.paths.prod) == env.first
    assert not (env.paths.releases / second).exists()


def test_unknown_ci_is_refused(env):
    second = env.upstream.commit("second")
    env.ci[second] = None
    with pytest.raises(DeployError):
        env.deployer.deploy(second)
    assert link_target(env.paths.prod) == env.first


def test_commit_outside_main_is_refused(env):
    env.upstream.branch("feature")
    feature = env.upstream.commit("feature")
    env.upstream.main()
    with pytest.raises(DeployError):
        env.deployer.deploy(feature)
    assert link_target(env.paths.prod) == env.first


def test_stopped_prod_is_refused(env):
    env.services.stop_marker("prod").touch()
    second = env.upstream.commit("second")
    with pytest.raises(DeployError):
        env.deployer.deploy(second)
    assert link_target(env.paths.prod) == env.first


def test_deploying_current_commit_is_noop(env):
    env.deployer.deploy(env.first)
    assert env.services.calls == []
    assert any("反映済み" in note for note in env.notes)


def test_rollback_swaps_current_and_previous(env):
    second = env.upstream.commit("second")
    env.deployer.deploy(second)
    env.deployer.rollback()
    assert link_target(env.paths.prod) == env.first
    assert link_target(env.paths.previous) == second


def test_rollback_without_previous_is_refused(env):
    with pytest.raises(DeployError):
        env.deployer.rollback()


def test_interrupted_deploy_is_recovered(env):
    second = env.upstream.commit("second")
    env.deployer.deploy(second)
    third = env.upstream.commit("third")
    env.deployer.sync_mirror()
    env.deployer.build_release(third)
    env.paths.journal.write_text(json.dumps({"previous": second, "target": third, "older": env.first}))
    env.deployer._point(env.paths.prod, third)
    env.deployer._point(env.paths.previous, second)
    env.deployer.recover()
    assert link_target(env.paths.prod) == second
    assert link_target(env.paths.previous) == env.first
    assert not env.paths.journal.exists()


def test_corrupt_journal_is_cleared(env):
    env.paths.journal.write_text("broken")
    env.deployer.recover()
    assert not env.paths.journal.exists()
    assert link_target(env.paths.prod) == env.first


def test_cleanup_keeps_prod_previous_and_staging(env):
    commits = [env.upstream.commit(f"c{i}") for i in range(5)]
    env.deployer.deploy_staging("main")
    staging = link_target(env.paths.staging)
    env.deployer.sync_mirror()
    for sha in commits[:4]:
        env.deployer.build_release(sha)
    env.deployer.cleanup()
    remaining = {p.name for p in env.paths.releases.iterdir()}
    assert link_target(env.paths.prod) in remaining
    assert staging in remaining
    assert len(remaining) <= 3


def test_staging_starts_branch(env):
    env.upstream.branch("feature/x")
    sha = env.upstream.commit("feature")
    env.upstream.main()
    env.deployer.deploy_staging("feature/x")
    assert link_target(env.paths.staging) == sha
    assert link_target(env.paths.prod) == env.first
    assert ("start_staging",) in env.services.calls
    assert ("restart", "prod") not in env.services.calls
    assert any("✅" in note for note in env.notes)


def test_staging_failure_stops_service(env):
    env.upstream.branch("feature")
    sha = env.upstream.commit("feature")
    env.upstream.main()
    env.services.unhealthy.add(sha)
    env.deployer.deploy_staging("feature")
    assert env.services.calls[-1] == ("stop_staging",)
    assert any("🚨" in note for note in env.notes)


def test_staging_missing_branch(env):
    with pytest.raises(DeployError):
        env.deployer.deploy_staging("missing")


def test_stop_staging_removes_link(env):
    env.deployer.deploy_staging("main")
    env.deployer.stop_staging()
    assert link_target(env.paths.staging) is None


@pytest.mark.parametrize("branch, valid", [
    ("main", True), ("feat/deploy-runner", True), ("v1.2", True),
    ("-x", False), ("a..b", False), ("a b", False), ("a;rm", False),
    ("a//b", False), ("a/", False), ("a.lock", False), ("", False), (None, False),
    ("$(id)", False), ("a" * 102, False),
])
def test_branch_validation(branch, valid):
    assert is_valid_branch(branch) is valid


def test_sha_validation():
    assert is_valid_sha("a" * 40)
    assert not is_valid_sha("a" * 39)
    assert not is_valid_sha("-" * 40)
    assert not is_valid_sha("A" * 40)


# ── runner ──

def _put(env, name, data):
    path = env.paths.requests / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def test_runner_processes_requests_in_order(env):
    second = env.upstream.commit("second")
    _put(env, "1.json", {"action": "deploy", "sha": second})
    _put(env, "2.json", {"action": "rollback"})
    runner.process_requests(env.deployer)
    assert link_target(env.paths.prod) == env.first
    assert list(env.paths.requests.iterdir()) == []


@pytest.mark.parametrize("data", [
    "not json",
    [],
    {"action": "unknown"},
    {"action": "deploy"},
    {"action": "deploy", "sha": "main; rm -rf /"},
    {"action": "deploy_staging", "branch": "../x"},
])
def test_runner_rejects_invalid_requests(env, data):
    _put(env, "1.json", data)
    runner.process_requests(env.deployer)
    assert env.services.calls == []
    assert list(env.paths.requests.iterdir()) == []
    assert any("不正な要求" in note for note in env.notes)


def test_request_is_removed_before_running(env):
    second = env.upstream.commit("second")
    _put(env, "1.json", {"action": "deploy", "sha": second})
    seen = []
    original = env.deployer.deploy

    def check(sha):
        seen.append(list(env.paths.requests.iterdir()))
        original(sha)

    env.deployer.deploy = check
    runner.process_requests(env.deployer)
    assert seen == [[]]


def test_unexpected_error_continues_with_next_request(env):
    _put(env, "1.json", {"action": "rollback"})
    _put(env, "2.json", {"action": "stop_staging"})
    env.deployer.rollback = MagicMock(side_effect=ValueError("internal secret"))
    runner.process_requests(env.deployer)
    assert ("stop_staging",) in env.services.calls
    assert not any("internal secret" in note for note in env.notes)


def test_telegram_notify_redacts_and_hides_token_on_failure(monkeypatch, capsys):
    import httpx
    monkeypatch.setattr(runner, "TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    monkeypatch.setattr(runner, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    post = MagicMock(side_effect=httpx.ConnectError("https://api.telegram.org/bot123456789:AAA"))
    monkeypatch.setattr(httpx, "post", post)
    runner.telegram_notify("連絡先 user@example.com")
    output = capsys.readouterr().out
    assert "user@example.com" not in output
    assert "123456789" not in output
    assert "user@example.com" not in post.call_args.kwargs["data"]["text"]


# ── Ops Botのコマンド ──

@pytest.fixture
def ops(tmp_path, monkeypatch):
    state = tmp_path / "agent" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("AGENT_STATE_DIR", str(state))
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    monkeypatch.setattr(bot, "approvals", ApprovalStore())
    return SimpleNamespace(state=state, root=state.parent)


def _update(user_id=USER):
    return SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock()), effective_user=SimpleNamespace(id=user_id),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _approve(update):
    data = update.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    query = SimpleNamespace(
        data=data, message=SimpleNamespace(reply_text=AsyncMock()),
        answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
    )
    asyncio.run(bot.on_decision(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER)), None,
    ))
    return query


def _requests(ops):
    directory = ops.state / "requests"
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))] if directory.exists() else []


SHA = "b" * 40


def _github(monkeypatch, ci="success"):
    monkeypatch.setattr(system, "github_main_status", lambda: {"sha": SHA, "commit": SHA[:7], "ci": ci})


def test_deploy_requires_approval_and_pins_commit(ops, monkeypatch):
    _github(monkeypatch)
    update = _update()
    asyncio.run(bot.request_deploy(update, _context()))
    assert _requests(ops) == []
    assert SHA[:7] in update.message.reply_text.call_args.args[0]
    _approve(update)
    assert _requests(ops) == [{"action": "deploy", "sha": SHA}]


@pytest.mark.parametrize("ci", ["failure", "in_progress", "未実行"])
def test_deploy_refused_unless_ci_succeeded(ops, monkeypatch, ci):
    _github(monkeypatch, ci)
    update = _update()
    asyncio.run(bot.request_deploy(update, _context()))
    assert "reply_markup" not in update.message.reply_text.call_args.kwargs
    assert "反映できません" in update.message.reply_text.call_args.args[0]


def test_deploy_when_github_unavailable(ops, monkeypatch):
    monkeypatch.setattr(system, "github_main_status", lambda: None)
    update = _update()
    asyncio.run(bot.request_deploy(update, _context()))
    assert "取得できません" in update.message.reply_text.call_args.args[0]


def test_deploy_when_already_current(ops, monkeypatch):
    _github(monkeypatch)
    (ops.root / "prod").symlink_to(ops.root / "releases" / SHA)
    update = _update()
    asyncio.run(bot.request_deploy(update, _context()))
    assert "反映済み" in update.message.reply_text.call_args.args[0]


def test_rollback_requires_previous_and_approval(ops):
    update = _update()
    asyncio.run(bot.request_rollback(update, _context()))
    assert "戻せるリリースがありません" in update.message.reply_text.call_args.args[0]

    (ops.root / "prod.previous").symlink_to(ops.root / "releases" / SHA)
    update = _update()
    asyncio.run(bot.request_rollback(update, _context()))
    assert _requests(ops) == []
    _approve(update)
    assert _requests(ops) == [{"action": "rollback"}]


@pytest.mark.parametrize("args", [[], ["a", "b"], ["../x"], ["-x"], ["$(id)"]])
def test_deploy_staging_rejects_bad_branch(ops, args):
    (ops.state / "staging.env").touch()
    update = _update()
    asyncio.run(bot.request_deploy_staging(update, _context(*args)))
    assert "使い方" in update.message.reply_text.call_args.args[0]
    assert _requests(ops) == []


def test_deploy_staging_requires_env_file(ops):
    update = _update()
    asyncio.run(bot.request_deploy_staging(update, _context("main")))
    assert "staging.env" in update.message.reply_text.call_args.args[0]


def test_deploy_staging_after_approval(ops):
    (ops.state / "staging.env").touch()
    update = _update()
    asyncio.run(bot.request_deploy_staging(update, _context("feat/x")))
    assert _requests(ops) == []
    _approve(update)
    assert _requests(ops) == [{"action": "deploy_staging", "branch": "feat/x"}]


def test_stop_staging_is_queued(ops):
    update = _update()
    asyncio.run(bot.stop_staging(update, _context()))
    assert _requests(ops) == [{"action": "stop_staging"}]


def test_enqueue_leaves_no_temporary_file_in_queue(ops):
    bot.enqueue("rollback")
    assert all(p.suffix == ".json" for p in (ops.state / "requests").iterdir())
    assert not list(ops.state.glob(".*.tmp"))


def test_health_check_quiet_during_deploy_and_alerts_when_stuck(ops, monkeypatch):
    monkeypatch.setattr(system, "service_state", lambda t: {"loaded": False, "running": False, "pid": None})
    monkeypatch.setattr(system, "memory_free_percent", lambda: 50)
    monkeypatch.setattr(system, "disk_free_gb", lambda: 100.0)
    journal = ops.state / "deploy-in-progress.json"
    journal.write_text("{}")
    assert bot.check_health() == set()
    old = journal.stat().st_mtime - bot.DEPLOY_STUCK_SECONDS - 1
    os.utime(journal, (old, old))
    assert bot.check_health() == {"反映の処理が終わっていない可能性があります"}
    journal.unlink()
    assert "本番Botが停止しています" in bot.check_health()
