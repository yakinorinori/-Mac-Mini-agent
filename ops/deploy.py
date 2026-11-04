"""リリースの作成、本番とstagingへの反映、切り戻し。"""
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agent.process import CommandError, run_command
from ops import system

REMOTE_URL = f"https://github.com/{system.GITHUB_REPOSITORY}.git"
HEALTH_TIMEOUT_SECONDS = 90
HEALTH_POLL_SECONDS = 5
KEEP_RELEASES = 3
INSTALL_TIMEOUT_SECONDS = 900
TEST_TIMEOUT_SECONDS = 300
_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
READY_MARKER = ".release-ready"


class DeployError(RuntimeError):
    """利用者へ理由を伝えて中止する反映の失敗。"""


def is_valid_sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA.match(value))


def is_valid_branch(value) -> bool:
    return (
        isinstance(value, str)
        and bool(_BRANCH.match(value))
        and ".." not in value
        and "//" not in value
        and not value.endswith(("/", ".lock"))
    )


def short(sha) -> str:
    return sha[:7] if sha else "なし"


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def state(self): return self.root / "state"
    @property
    def requests(self): return self.state / "requests"
    @property
    def releases(self): return self.root / "releases"
    @property
    def mirror(self): return self.root / "repo.git"
    @property
    def prod(self): return self.root / "prod"
    @property
    def previous(self): return self.root / "prod.previous"
    @property
    def staging(self): return self.root / "staging"
    @property
    def journal(self): return self.state / "deploy-in-progress.json"
    @property
    def history(self): return self.state / "deploy-history.jsonl"
    @property
    def lock(self): return self.state / "runner.lock"


def default_paths() -> Paths:
    return Paths(system.state_dir().parent)


def link_target(link: Path):
    """リンク先のコミットを返す。リンクがなければ None を返す。"""

    try:
        return Path(os.readlink(link)).name
    except OSError:
        return None


class Deployer:

    def __init__(
        self,
        paths: Paths,
        notify=print,
        ci_conclusion=None,
        services=system,
        clock=time.time,
        sleep=time.sleep,
        remote=REMOTE_URL,
    ):
        self.paths = paths
        self.notify = notify
        self.ci_conclusion = ci_conclusion or system.ci_conclusion
        self.services = services
        self.clock = clock
        self.sleep = sleep
        self.remote = remote

    # ── Git ──

    def _git(self, *args, timeout=300):
        return run_command(["git", "-C", str(self.paths.mirror), *args], timeout=timeout)

    def sync_mirror(self):
        self.paths.root.mkdir(parents=True, exist_ok=True)

        if self.paths.mirror.exists():
            self._git("fetch", "--prune", "origin")
        else:
            run_command(["git", "clone", "--mirror", self.remote, str(self.paths.mirror)], timeout=300)

    def resolve(self, branch: str) -> str:
        try:
            return self._git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").stdout.strip()
        except CommandError as e:
            raise DeployError(f"ブランチが見つかりません: {branch}") from e

    def _is_on_main(self, sha: str) -> bool:
        try:
            self._git("merge-base", "--is-ancestor", sha, "refs/heads/main")
            return True
        except CommandError:
            return False

    # ── リリース ──

    def release_path(self, sha: str) -> Path:
        return self.paths.releases / sha

    def _prepare_release(self, path: Path):
        """依存関係を入れてテストする。実行環境の設定は引き継がない。"""

        python = path / ".venv" / "bin" / "python"
        run_command([sys.executable, "-m", "venv", str(path / ".venv")], timeout=300)
        run_command(
            [str(python), "-m", "pip", "install", "--quiet", "-r", str(path / "requirements-dev.txt")],
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        run_command(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            timeout=TEST_TIMEOUT_SECONDS,
            cwd=path,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
                "LANG": "en_US.UTF-8",
            },
        )

    def _remove_release(self, sha: str):
        path = self.release_path(sha)

        try:
            self._git("worktree", "remove", "--force", str(path))
        except CommandError:
            shutil.rmtree(path, ignore_errors=True)
            self._git("worktree", "prune")

    def build_release(self, sha: str) -> Path:
        path = self.release_path(sha)

        if (path / READY_MARKER).is_file():
            return path

        if path.exists():
            self._remove_release(sha)

        self.paths.releases.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--detach", str(path), sha)

        try:
            self._prepare_release(path)
        except Exception:
            self._remove_release(sha)
            raise

        (path / READY_MARKER).touch()
        return path

    def _protected_releases(self) -> set:
        return {
            target
            for target in (
                link_target(self.paths.prod),
                link_target(self.paths.previous),
                link_target(self.paths.staging),
            )
            if target
        }

    def cleanup(self):
        if not self.paths.releases.is_dir():
            return

        protected = self._protected_releases()
        releases = sorted(
            (p for p in self.paths.releases.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removable = [p for p in releases if p.name not in protected]
        allowed_extra = max(0, KEEP_RELEASES - len(protected))

        for path in removable[allowed_extra:]:
            self._remove_release(path.name)

    # ── リンクと記録 ──

    def _point(self, link: Path, sha: str):
        temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(self.release_path(sha))
        os.replace(temporary, link)

    def _record(self, event: str, **fields):
        self.paths.state.mkdir(parents=True, exist_ok=True)

        with self.paths.history.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"time": self.clock(), "event": event, **fields}) + "\n")

    def _write_journal(self, previous, target):
        self.paths.journal.write_text(
            json.dumps({"previous": previous, "target": target, "time": self.clock()}),
            encoding="utf-8",
        )

    # ── 起動確認 ──

    def wait_healthy(self, name: str, sha: str, since: float) -> bool:
        """同じプロセスのheartbeatが2回更新されたら正常とする。再起動の繰り返しを除外するため。"""

        deadline = self.clock() + HEALTH_TIMEOUT_SECONDS
        first = None

        while self.clock() < deadline:
            beat = self.services.read_heartbeat(name)

            if beat and beat.get("time", 0) >= since and sha.startswith(str(beat.get("commit")) or "?"):
                if first is None or beat.get("pid") != first.get("pid"):
                    first = beat
                elif beat["time"] > first["time"]:
                    return True

            self.sleep(HEALTH_POLL_SECONDS)

        return False

    def _restart_prod_and_wait(self, sha: str) -> bool:
        since = self.clock()
        self.services.restart_service("prod")
        return self.wait_healthy("prod", sha, since)

    # ── 本番 ──

    def initialize(self, ref: str = "main"):
        """本番リンクがまだない時に、最初のリリースを作る。"""

        self.sync_mirror()
        sha = self.resolve(ref)
        self._require_ci(sha)
        self.build_release(sha)
        self._point(self.paths.prod, sha)
        self._record("initialize", commit=sha)
        return sha

    def _require_ci(self, sha: str):
        conclusion = self.ci_conclusion(sha)

        if conclusion != "success":
            raise DeployError(f"{short(sha)} のCIが成功していません（{conclusion or '確認できません'}）")

    def deploy(self, sha: str):
        if self.services.stop_marker("prod").exists():
            raise DeployError("本番Botは停止中です。/start_prod の後でもう一度実行してください。")

        self.sync_mirror()

        if not self._is_on_main(sha):
            raise DeployError(f"{short(sha)} は main にありません")

        self._require_ci(sha)
        current = link_target(self.paths.prod)

        if current == sha:
            self.notify(f"ℹ️ {short(sha)} は反映済みです。")
            return

        self.notify(f"🚀 {short(sha)} のリリースを準備しています。")

        try:
            self.build_release(sha)
        except CommandError as e:
            self._record("deploy_build_failed", commit=sha)
            raise DeployError(f"{short(sha)} の準備に失敗しました。本番は変更していません。\n{e}") from e

        self._write_journal(current, sha)

        if current:
            self._point(self.paths.previous, current)

        self._point(self.paths.prod, sha)

        try:
            healthy = self._restart_prod_and_wait(sha)
        except CommandError:
            healthy = False

        if healthy:
            self.paths.journal.unlink(missing_ok=True)
            self._record("deploy_succeeded", commit=sha, previous=current)
            self.cleanup()
            self.notify(f"✅ {short(sha)} を本番へ反映しました（直前: {short(current)}）。")
            return

        self._record("deploy_unhealthy", commit=sha, previous=current)
        self._revert_after_failure(current, sha)

    def _revert_after_failure(self, previous, failed):
        if not previous:
            self.paths.journal.unlink(missing_ok=True)
            self.notify(f"🚨 {short(failed)} が起動できず、戻せるリリースもありません。")
            return

        self._point(self.paths.prod, previous)

        try:
            healthy = self._restart_prod_and_wait(previous)
        except CommandError:
            healthy = False

        self.paths.journal.unlink(missing_ok=True)
        self._record("deploy_reverted", commit=previous, failed=failed, healthy=healthy)

        if healthy:
            self.notify(
                f"↩️ {short(failed)} の起動を確認できなかったので、{short(previous)} へ戻しました。\n"
                "/logs prod で原因を確認できます。"
            )
        else:
            self.notify(
                f"🚨 {short(failed)} の起動に失敗し、{short(previous)} へ戻しても起動を確認できません。"
                "/logs prod を確認してください。"
            )

    def rollback(self):
        current = link_target(self.paths.prod)
        previous = link_target(self.paths.previous)

        if not previous or not self.release_path(previous).is_dir():
            raise DeployError("戻せるリリースがありません。")

        if self.services.stop_marker("prod").exists():
            raise DeployError("本番Botは停止中です。/start_prod の後でもう一度実行してください。")

        self._write_journal(current, previous)
        self._point(self.paths.prod, previous)
        self._point(self.paths.previous, current)

        try:
            healthy = self._restart_prod_and_wait(previous)
        except CommandError:
            healthy = False

        self.paths.journal.unlink(missing_ok=True)
        self._record("rollback", commit=previous, previous=current, healthy=healthy)

        if healthy:
            self.notify(f"↩️ {short(previous)} へ戻しました（戻す前: {short(current)}）。")
        else:
            self.notify(f"🚨 {short(previous)} へ戻しましたが、起動を確認できません。/logs prod を確認してください。")

    def recover(self):
        """反映の途中でrunnerが止まっていた場合に、直前のリリースへ戻す。"""

        try:
            journal = json.loads(self.paths.journal.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except ValueError:
            journal = {}

        previous = journal.get("previous")

        if is_valid_sha(previous) and self.release_path(previous).is_dir():
            self.notify(f"⚠️ 前回の反映が中断していたので、{short(previous)} へ戻します。")
            self._revert_after_failure(previous, journal.get("target"))
        else:
            self.paths.journal.unlink(missing_ok=True)
            self.notify("⚠️ 前回の反映が中断していました。/status で状態を確認してください。")

    # ── staging ──

    def deploy_staging(self, branch: str):
        if not is_valid_branch(branch):
            raise DeployError("ブランチ名が不正です")

        self.sync_mirror()
        sha = self.resolve(branch)
        self.notify(f"🧪 {branch}（{short(sha)}）を準備しています。")

        try:
            self.build_release(sha)
        except CommandError as e:
            raise DeployError(f"{branch} の準備に失敗しました。\n{e}") from e

        self._point(self.paths.staging, sha)
        since = self.clock()
        self.services.stop_staging_service()
        self.services.start_staging_service()

        if self.wait_healthy("staging", sha, since):
            self._record("staging_started", commit=sha, branch=branch)
            self.notify(f"✅ stagingで {branch}（{short(sha)}）が起動しました。確認後は /stop_staging で止めてください。")
            return

        self.services.stop_staging_service()
        self._record("staging_failed", commit=sha, branch=branch)
        self.notify(f"🚨 stagingで {short(sha)} の起動を確認できませんでした。/logs staging で確認できます。")

    def stop_staging(self):
        self.services.stop_staging_service()
        self.paths.staging.unlink(missing_ok=True)
        self.notify("🛑 stagingを停止しました。")
