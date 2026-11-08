"""承認待ち、予約中の遅延実行、実行記録をSQLiteへ保存する。

再起動しても承認ボタンと予約が使えるようにする。
承認待ちと予約にはツールの引数（メール本文など）が入るので、決定や実行の時点で削除する。
実行記録にはツール名と結果だけを残し、引数や依頼文は残さない。
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from agent.models import Plan, Step
from approvals import DEFAULT_TTL_SECONDS, PendingApproval

DB_NAME = "agent.db"
RUN_RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    plan TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    run_at REAL NOT NULL,
    text TEXT NOT NULL,
    plan TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    tools TEXT NOT NULL,
    status TEXT NOT NULL,
    seconds REAL NOT NULL
);
"""

# 2-2で追加した列。既存のデータベースには起動時に追加する
_RUN_COLUMNS = {
    "route": "TEXT NOT NULL DEFAULT ''",
    "model": "TEXT NOT NULL DEFAULT ''",
    "plan_seconds": "REAL NOT NULL DEFAULT 0",
    "text": "TEXT",
    "text_length": "INTEGER NOT NULL DEFAULT 0",
}

# 依頼文を残さない条件。メール本文や宛先、開発の指示が入りうるため
_SENSITIVE_TOOLS = {"mail_send", "send_to_vscode_chat"}
_SENSITIVE_TEXT = re.compile(r"メール|mail|@|送信|vs ?code|開発", re.IGNORECASE)
MAX_LOGGED_TEXT = 200


def loggable_text(text, plan):
    """振り分けの分析に使える依頼文だけを返す。本文を含みうる依頼はNoneにする。"""

    if not text or _SENSITIVE_TEXT.search(text):
        return None

    if plan is not None and any(step.tool in _SENSITIVE_TOOLS for step in plan.steps):
        return None

    return text[:MAX_LOGGED_TEXT]


def plan_to_json(plan: Plan) -> str:
    return json.dumps(asdict(plan), ensure_ascii=False)


def plan_from_json(data: str) -> Plan:
    raw = json.loads(data)

    return Plan(
        steps=[Step(**step) for step in raw["steps"]],
        message=raw.get("message", ""),
        delay=raw.get("delay"),
        preapproved=raw.get("preapproved", False),
        route=raw.get("route", ""),
        model=raw.get("model", ""),
        plan_seconds=raw.get("plan_seconds", 0.0),
    )


class Store:

    def __init__(self, path=":memory:", clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()

        if path != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # 本人だけが読めるファイルとして作る
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            os.chmod(path, 0o600)

        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)

        with self._lock:
            self._db.executescript(_SCHEMA)
            existing = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}

            for name, definition in _RUN_COLUMNS.items():
                if name not in existing:
                    self._db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            self._db.execute(
                "DELETE FROM runs WHERE time < ?",
                (self._clock() - RUN_RETENTION_DAYS * 86400,),
            )

    def _execute(self, sql, parameters=()):
        with self._lock:
            return self._db.execute(sql, parameters)

    # ── 承認待ち ──

    def add_approval(self, plan, text, user_id, expires_at) -> str:
        approval_id = secrets.token_urlsafe(16)

        with self._lock:
            self._db.execute("DELETE FROM approvals WHERE expires_at <= ?", (self._clock(),))
            self._db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?)",
                (approval_id, user_id, text, plan_to_json(plan), expires_at),
            )

        return approval_id

    def take_approval(self, approval_id, user_id):
        """本人の有効な承認待ちを一度だけ取り出す。"""

        with self._lock:
            row = self._db.execute(
                "SELECT user_id, text, plan, expires_at FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()

            if row is None or row[0] != user_id:
                return None

            self._db.execute("DELETE FROM approvals WHERE id = ?", (approval_id,))

        if self._clock() >= row[3]:
            return None

        return PendingApproval(plan=plan_from_json(row[2]), text=row[1], user_id=row[0], expires_at=row[3])

    def remove_expired_approvals(self):
        self._execute("DELETE FROM approvals WHERE expires_at <= ?", (self._clock(),))

    # ── 予約中の遅延実行 ──

    def add_scheduled(self, user_id, chat_id, run_at, text, plan) -> int:
        cursor = self._execute(
            "INSERT INTO scheduled (user_id, chat_id, run_at, text, plan) VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, run_at, text, plan_to_json(plan)),
        )
        return cursor.lastrowid

    def claim_scheduled(self, job_id) -> bool:
        """実行の直前に予約を削除する。取り消し済みならFalseを返す。"""

        return self._execute("DELETE FROM scheduled WHERE id = ?", (job_id,)).rowcount == 1

    def cancel_scheduled(self, user_id) -> int:
        return self._execute("DELETE FROM scheduled WHERE user_id = ?", (user_id,)).rowcount

    def list_scheduled(self) -> list[dict]:
        rows = self._execute(
            "SELECT id, user_id, chat_id, run_at, text, plan FROM scheduled ORDER BY run_at"
        ).fetchall()

        return [
            {
                "id": row[0], "user_id": row[1], "chat_id": row[2],
                "run_at": row[3], "text": row[4], "plan": plan_from_json(row[5]),
            }
            for row in rows
        ]

    # ── 実行記録 ──

    def record_run(self, plan, status, seconds, text=None, route=None):
        """経路、モデル、ツール名、結果、所要時間を記録する。引数は残さない。"""

        steps = plan.steps if plan is not None else []
        self._execute(
            "INSERT INTO runs (time, tools, status, seconds, route, model, plan_seconds, text, text_length)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock(),
                json.dumps([step.tool for step in steps]),
                status,
                round(seconds, 2),
                route or (plan.route if plan is not None else "") or "none",
                plan.model if plan is not None else "",
                round(plan.plan_seconds, 2) if plan is not None else 0,
                loggable_text(text, plan),
                len(text or ""),
            ),
        )

    def recent_runs(self, limit=20) -> list[dict]:
        rows = self._execute(
            "SELECT time, tools, status, seconds, route, model, plan_seconds, text"
            " FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "time": r[0], "tools": json.loads(r[1]), "status": r[2], "seconds": r[3],
                "route": r[4], "model": r[5], "plan_seconds": r[6], "text": r[7],
            }
            for r in rows
        ]


class StoredApprovals:
    """ApprovalStoreと同じ使い方で、承認待ちをSQLiteに保存する。"""

    def __init__(self, store: Store, ttl_seconds=DEFAULT_TTL_SECONDS, clock=time.time):
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        store.remove_expired_approvals()

    def add(self, plan, text, user_id) -> str:
        return self._store.add_approval(plan, text, user_id, self._clock() + self._ttl_seconds)

    def take(self, approval_id, user_id):
        return self._store.take_approval(approval_id, user_id)


def summarize_runs(path, since: float) -> list[dict]:
    """経路ごとの件数、平均の計画時間と実行時間、失敗数を返す。読み取り専用で開く。"""

    path = Path(path)

    if not path.is_file():
        return []

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}

        if "route" not in columns:
            return []

        rows = connection.execute(
            "SELECT route, model, COUNT(*), AVG(plan_seconds), AVG(seconds),"
            " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)"
            " FROM runs WHERE time >= ? GROUP BY route, model ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    finally:
        connection.close()

    return [
        {
            "route": r[0], "model": r[1], "count": r[2],
            "plan_seconds": r[3] or 0, "run_seconds": r[4] or 0, "failed": r[5],
        }
        for r in rows
    ]


def open_store(directory) -> Store:
    """状態フォルダがあればファイルへ、なければメモリ上に保存する。"""

    return Store(Path(directory) / DB_NAME) if directory else Store()
