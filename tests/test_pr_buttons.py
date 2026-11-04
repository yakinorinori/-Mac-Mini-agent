"""PR通知とボタン操作の回帰テスト(GitHubとTelegram通信はモックする)。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ops import bot, github, runner, system
from ops.deploy import DeployError

USER = 111
OTHER = 222
SHA = "a" * 40
NEW_SHA = "b" * 40


def _pull(number=12, sha=SHA, state="open", same_repo=True, draft=False):
    return {
        "number": number, "title": "修正", "url": f"https://github.com/x/pull/{number}",
        "branch": "feat/x", "sha": sha, "state": state, "same_repo": same_repo, "draft": draft,
    }


@pytest.fixture
def ops(tmp_path, monkeypatch):
    state = tmp_path / "agent" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("AGENT_STATE_DIR", str(state))
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secretvalue")
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    return SimpleNamespace(state=state, root=state.parent)


def _requests(ops):
    directory = ops.state / "requests"
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))] if directory.exists() else []


def _github(monkeypatch, pulls, ci="success"):
    monkeypatch.setattr(github, "open_pull_requests", lambda: pulls)
    monkeypatch.setattr(github, "ci_conclusion", lambda sha: ci)


# ── 通知 ──

def test_finished_pr_is_notified_once_even_after_restart(ops, monkeypatch):
    _github(monkeypatch, [_pull()])
    assert [p["number"] for p, _ in bot.collect_pr_notices()] == [12]
    assert bot.collect_pr_notices() == []
    assert (ops.state / bot.PR_NOTICES_FILE).is_file()


def test_new_commit_is_notified_again(ops, monkeypatch):
    _github(monkeypatch, [_pull()])
    bot.collect_pr_notices()
    _github(monkeypatch, [_pull(sha=NEW_SHA)])
    assert len(bot.collect_pr_notices()) == 1


@pytest.mark.parametrize("ci", ["未実行", "queued", "in_progress"])
def test_running_ci_is_not_notified_yet(ops, monkeypatch, ci):
    _github(monkeypatch, [_pull()], ci)
    assert bot.collect_pr_notices() == []
    _github(monkeypatch, [_pull()], "success")
    assert len(bot.collect_pr_notices()) == 1


@pytest.mark.parametrize("pull", [_pull(same_repo=False), _pull(draft=True)])
def test_fork_and_draft_prs_are_ignored(ops, monkeypatch, pull):
    _github(monkeypatch, [pull])
    assert bot.collect_pr_notices() == []


def test_failed_ci_is_notified_without_buttons(ops, monkeypatch):
    _github(monkeypatch, [_pull()], "failure")
    send = AsyncMock()
    asyncio.run(bot.send_pr_notices(SimpleNamespace(send_message=send)))
    assert "failure" in send.call_args.kwargs["text"]
    assert send.call_args.kwargs["reply_markup"] is None


def test_successful_ci_has_all_buttons(ops, monkeypatch):
    _github(monkeypatch, [_pull()])
    send = AsyncMock()
    asyncio.run(bot.send_pr_notices(SimpleNamespace(send_message=send)))
    rows = send.call_args.kwargs["reply_markup"].inline_keyboard
    data = [row[0].callback_data for row in rows]
    assert data == [f"pr_staging:12:{SHA[:12]}", f"pr_deploy:12:{SHA[:12]}", f"pr_close:12:{SHA[:12]}"]
    assert all(len(item.encode()) <= 64 for item in data)


def test_corrupt_notice_file_is_ignored(ops, monkeypatch):
    (ops.state / bot.PR_NOTICES_FILE).write_text("broken")
    _github(monkeypatch, [_pull()])
    assert len(bot.collect_pr_notices()) == 1


# ── ボタン ──

def _press(data, user_id=USER, handler=None):
    query = SimpleNamespace(
        data=data,
        message=SimpleNamespace(reply_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
    asyncio.run((handler or bot.on_pr_action)(update, None))
    return query


def _current_pull(monkeypatch, pull=None):
    monkeypatch.setattr(github, "get_pull_request", lambda number: pull or _pull(number))


def test_staging_button_queues_branch_and_keeps_buttons(ops, monkeypatch):
    (ops.state / "staging.env").touch()
    _current_pull(monkeypatch)
    query = _press(f"pr_staging:12:{SHA[:12]}")
    assert _requests(ops) == [{"action": "deploy_staging", "branch": "feat/x"}]
    query.edit_message_reply_markup.assert_not_called()


def test_staging_button_requires_env_file(ops, monkeypatch):
    _current_pull(monkeypatch)
    query = _press(f"pr_staging:12:{SHA[:12]}")
    assert _requests(ops) == []
    assert "staging.env" in query.message.reply_text.call_args.args[0]


def test_deploy_button_pins_full_commit_and_removes_buttons(ops, monkeypatch):
    _current_pull(monkeypatch)
    query = _press(f"pr_deploy:12:{SHA[:12]}")
    assert _requests(ops) == [{"action": "merge_deploy", "number": 12, "sha": SHA}]
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_close_button_closes_pr(ops, monkeypatch):
    _current_pull(monkeypatch)
    close = MagicMock()
    monkeypatch.setattr(github, "close_pull_request", close)
    _press(f"pr_close:12:{SHA[:12]}")
    close.assert_called_once_with(12)
    assert _requests(ops) == []


@pytest.mark.parametrize("pull", [_pull(sha=NEW_SHA), _pull(state="closed"), _pull(same_repo=False)])
@pytest.mark.parametrize("action", ["pr_staging", "pr_deploy", "pr_close"])
def test_stale_buttons_do_nothing(ops, monkeypatch, pull, action):
    (ops.state / "staging.env").touch()
    _current_pull(monkeypatch, pull)
    close = MagicMock()
    monkeypatch.setattr(github, "close_pull_request", close)
    query = _press(f"{action}:12:{SHA[:12]}")
    assert _requests(ops) == []
    close.assert_not_called()
    assert "/prs" in query.message.reply_text.call_args.args[0]


def test_other_user_cannot_press_buttons(ops, monkeypatch):
    lookup = MagicMock()
    monkeypatch.setattr(github, "get_pull_request", lookup)
    query = _press(f"pr_deploy:12:{SHA[:12]}", user_id=OTHER)
    lookup.assert_not_called()
    query.message.reply_text.assert_not_called()
    assert _requests(ops) == []


@pytest.mark.parametrize("data", [
    "pr_deploy:0:aaaaaaaaaaaa", "pr_deploy:12:short", "pr_deploy:x:aaaaaaaaaaaa", "pr_unknown:12:aaaaaaaaaaaa",
    "pr_deploy:12:aaaaaaaaaaaa:extra",
])
def test_malformed_button_data_is_rejected(ops, monkeypatch, data):
    lookup = MagicMock()
    monkeypatch.setattr(github, "get_pull_request", lookup)
    _press(data)
    lookup.assert_not_called()
    assert _requests(ops) == []


def test_github_error_is_reported(ops, monkeypatch):
    monkeypatch.setattr(github, "get_pull_request", MagicMock(side_effect=github.GitHubError("だめ")))
    query = _press(f"pr_deploy:12:{SHA[:12]}")
    assert "だめ" in query.message.reply_text.call_args.args[0]


def _prod(ops, sha):
    (ops.root / "prod").symlink_to(ops.root / "releases" / sha)


def test_rollback_button_queues_when_prod_unchanged(ops):
    _prod(ops, SHA)
    _press(f"rollback_to:{SHA[:12]}", handler=bot.on_rollback_button)
    assert _requests(ops) == [{"action": "rollback"}]


def test_old_rollback_button_does_nothing(ops):
    _prod(ops, NEW_SHA)
    query = _press(f"rollback_to:{SHA[:12]}", handler=bot.on_rollback_button)
    assert _requests(ops) == []
    assert "戻しません" in query.message.reply_text.call_args.args[0]


def test_other_user_cannot_rollback(ops):
    _prod(ops, SHA)
    _press(f"rollback_to:{SHA[:12]}", user_id=OTHER, handler=bot.on_rollback_button)
    assert _requests(ops) == []


def test_prs_command_without_token(ops, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(bot.show_prs(update, None))
    assert "GITHUB_TOKEN" in update.message.reply_text.call_args.args[0]


def test_prs_command_lists_with_buttons_only_on_success(ops, monkeypatch):
    monkeypatch.setattr(github, "open_pull_requests", lambda: [_pull(12, SHA), _pull(13, NEW_SHA)])
    monkeypatch.setattr(github, "ci_conclusion", lambda sha: "success" if sha == SHA else "in_progress")
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(bot.show_prs(update, None))
    calls = update.message.reply_text.call_args_list
    assert calls[0].kwargs["reply_markup"] is not None
    assert calls[1].kwargs["reply_markup"] is None


# ── マージと反映 ──

class FakeDeployer:
    def __init__(self, tmp_path, ci=None):
        self.paths = SimpleNamespace(
            prod=tmp_path / "prod", staging=tmp_path / "staging",
        )
        self.notes = []
        self.notify = self.notes.append
        self.deployed = []
        self.stopped = 0
        self.now = 0
        self.ci_values = iter(["success"] if ci is None else ci)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def ci_conclusion(self, sha):
        return next(self.ci_values, "in_progress")

    def deploy(self, sha):
        self.deployed.append(sha)
        self.paths.prod.unlink(missing_ok=True)
        self.paths.prod.symlink_to(f"/x/{sha}")

    def stop_staging(self):
        self.stopped += 1


def _api(pull=None, ci="success", merged=NEW_SHA):
    return SimpleNamespace(
        get_pull_request=MagicMock(return_value=pull or _pull()),
        ci_conclusion=MagicMock(return_value=ci),
        merge_pull_request=MagicMock(return_value=merged),
    )


def test_merge_and_deploy_uses_merge_commit(tmp_path):
    deployer = FakeDeployer(tmp_path, ci=["in_progress", "success"])
    api = _api()
    assert runner.merge_and_deploy(12, SHA, deployer, api) == NEW_SHA
    api.merge_pull_request.assert_called_once_with(12, SHA)
    assert deployer.deployed == [NEW_SHA]


@pytest.mark.parametrize("pull, ci", [
    (_pull(sha=NEW_SHA), "success"),
    (_pull(state="closed"), "success"),
    (_pull(same_repo=False), "success"),
    (_pull(), "failure"),
    (_pull(), "in_progress"),
])
def test_merge_refused_when_pr_changed_or_ci_not_success(tmp_path, pull, ci):
    deployer = FakeDeployer(tmp_path)
    api = _api(pull, ci)
    with pytest.raises(DeployError):
        runner.merge_and_deploy(12, SHA, deployer, api)
    api.merge_pull_request.assert_not_called()
    assert deployer.deployed == []


def test_failed_main_ci_stops_before_deploy(tmp_path):
    deployer = FakeDeployer(tmp_path, ci=["failure"])
    with pytest.raises(DeployError) as e:
        runner.merge_and_deploy(12, SHA, deployer, _api())
    assert "本番は変更していません" in str(e.value)
    assert deployer.deployed == []


def test_slow_main_ci_times_out_without_deploy(tmp_path):
    deployer = FakeDeployer(tmp_path, ci=[])
    with pytest.raises(DeployError):
        runner.merge_and_deploy(12, SHA, deployer, _api())
    assert deployer.deployed == []


def test_staging_stops_after_production_deploy(tmp_path):
    deployer = FakeDeployer(tmp_path)
    deployer.paths.staging.symlink_to("/x/s")
    runner.merge_and_deploy(12, SHA, deployer, _api())
    assert deployer.stopped == 1


def test_rollback_is_offered_only_when_deploy_stayed_live(tmp_path):
    deployer = FakeDeployer(tmp_path)
    offer = MagicMock()
    runner.execute({"action": "merge_deploy", "number": 12, "sha": SHA}, deployer, _api(), offer)
    offer.assert_called_once_with(NEW_SHA)

    other = tmp_path / "reverted"
    other.mkdir()
    reverted = FakeDeployer(other)
    reverted.deploy = lambda sha: None
    offer = MagicMock()
    runner.execute({"action": "deploy", "sha": SHA}, reverted, _api(), offer)
    offer.assert_not_called()


@pytest.mark.parametrize("request_data", [
    {"action": "merge_deploy", "number": "12", "sha": SHA},
    {"action": "merge_deploy", "number": True, "sha": SHA},
    {"action": "merge_deploy", "number": 0, "sha": SHA},
    {"action": "merge_deploy", "number": 12, "sha": "main"},
])
def test_invalid_merge_request_is_rejected(tmp_path, request_data):
    api = _api()
    with pytest.raises(DeployError):
        runner.execute(request_data, FakeDeployer(tmp_path), api, MagicMock())
    api.merge_pull_request.assert_not_called()


def test_rollback_offer_button_data(monkeypatch):
    send = MagicMock()
    monkeypatch.setattr(runner, "telegram_send", send)
    runner.offer_rollback(SHA)
    assert send.call_args.args[1][0][0]["callback_data"] == f"rollback_to:{SHA[:12]}"


# ── GitHub API ──

def _response(status, data):
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://api.github.com/x"))


def test_token_is_sent_only_as_header(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secretvalue")
    request = MagicMock(return_value=_response(200, {"check_runs": []}))
    monkeypatch.setattr(httpx, "request", request)
    assert github.ci_conclusion(SHA) == "未実行"
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer github_pat_secretvalue"
    assert "secret" not in request.call_args.args[1]


def test_errors_do_not_include_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secretvalue")
    monkeypatch.setattr(httpx, "request", MagicMock(return_value=_response(403, {"message": "denied"})))
    with pytest.raises(github.GitHubError) as e:
        github.get_pull_request(12)
    assert "403" in str(e.value) and "secret" not in str(e.value)


def test_connection_error_is_wrapped(monkeypatch):
    monkeypatch.setattr(httpx, "request", MagicMock(side_effect=httpx.ConnectError("https://secret")))
    with pytest.raises(github.GitHubError) as e:
        github.open_pull_requests()
    assert "secret" not in str(e.value)


def test_merge_sends_expected_commit(monkeypatch):
    request = MagicMock(return_value=_response(200, {"sha": NEW_SHA}))
    monkeypatch.setattr(httpx, "request", request)
    assert github.merge_pull_request(12, SHA) == NEW_SHA
    assert request.call_args.kwargs["json"]["sha"] == SHA


def test_summary_detects_forks(monkeypatch):
    data = {
        "number": 1, "title": "t", "html_url": "u", "state": "open",
        "head": {"ref": "b", "sha": SHA, "repo": {"full_name": "other/repo"}},
    }
    monkeypatch.setattr(httpx, "request", MagicMock(return_value=_response(200, data)))
    assert github.get_pull_request(1)["same_repo"] is False


def test_fine_grained_token_is_redacted():
    assert "github_pat_" not in system.redact("x github_pat_abcdefghijklmnopqrstuvwxyz y")
