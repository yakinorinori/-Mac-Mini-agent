"""GitHubのPR操作。トークンをログやメッセージへ出さない。"""
import os

import httpx

from ops.system import GITHUB_REPOSITORY

API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
TIMEOUT_SECONDS = 15


class GitHubError(RuntimeError):
    """GitHub APIの操作に失敗した。"""


def has_token() -> bool:
    return bool(os.getenv("GITHUB_TOKEN"))


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def _request(method: str, path: str, **kwargs):
    try:
        response = httpx.request(
            method, f"{API}{path}", headers=_headers(), timeout=TIMEOUT_SECONDS, **kwargs,
        )
    except httpx.HTTPError as e:
        raise GitHubError(f"GitHubへ接続できません（{type(e).__name__}）") from e

    if response.status_code >= 400:
        try:
            detail = str(response.json().get("message", ""))[:200]
        except ValueError:
            detail = ""
        raise GitHubError(f"GitHubの操作に失敗しました（{response.status_code}）: {detail}")

    return response.json()


def _summary(data: dict) -> dict:
    head = data["head"]
    head_repo = head.get("repo") or {}

    return {
        "number": data["number"],
        "title": data["title"],
        "url": data["html_url"],
        "branch": head["ref"],
        "sha": head["sha"],
        "state": data["state"],
        "draft": bool(data.get("draft")),
        # フォークのPRは実行しない
        "same_repo": head_repo.get("full_name", "").lower() == GITHUB_REPOSITORY.lower(),
    }


def open_pull_requests() -> list[dict]:
    return [_summary(item) for item in _request("GET", "/pulls?state=open&per_page=30")]


def get_pull_request(number: int) -> dict:
    return _summary(_request("GET", f"/pulls/{number}"))


def ci_conclusion(sha: str):
    """テストの結果を返す。未実行は "未実行"、実行中は状態名を返す。"""

    runs = [
        run for run in _request("GET", f"/commits/{sha}/check-runs").get("check_runs", [])
        if run.get("name") == "test"
    ]

    return (runs[0].get("conclusion") or runs[0].get("status")) if runs else "未実行"


def merge_pull_request(number: int, sha: str) -> str:
    """確認したコミットのままならマージし、マージコミットを返す。"""

    data = _request(
        "PUT", f"/pulls/{number}/merge",
        json={"sha": sha, "merge_method": "merge", "commit_title": f"Merge pull request #{number}"},
    )
    return data["sha"]


def close_pull_request(number: int):
    _request("PATCH", f"/pulls/{number}", json={"state": "closed"})
