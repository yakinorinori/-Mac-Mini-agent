"""承認待ち、予約中の遅延実行、実行記録をSQLiteへ保存する。

再起動しても承認ボタンと予約が使えるようにする。
承認待ちと予約にはツールの引数（メール本文など）が入るので、決定や実行の時点で削除する。
実行記録にはツール名と結果だけを残し、引数や依頼文は残さない。
"""
import json
import os
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


def plan_to_json(plan: Plan) -> str:
    return json.dumps(asdict(plan), ensure_ascii=False)


def plan_from_json(data: str) -> Plan:
    raw = json.loads(data)

    return Plan(
        steps=[Step(**step) for step in raw["steps"]],
        message=raw.get("message", ""),
        delay=raw.get("delay"),
        preapproved=raw.get("preapproved", False),
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

    def record_run(self, plan, status, seconds):
        tools = json.dumps([step.tool for step in plan.steps])
        self._execute(
            "INSERT INTO runs (time, tools, status, seconds) VALUES (?, ?, ?, ?)",
            (self._clock(), tools, status, round(seconds, 2)),
        )

    def recent_runs(self, limit=20) -> list[dict]:
        rows = self._execute(
            "SELECT time, tools, status, seconds FROM runs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [{"time": r[0], "tools": json.loads(r[1]), "status": r[2], "seconds": r[3]} for r in rows]


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


def open_store(directory) -> Store:
    """状態フォルダがあればファイルへ、なければメモリ上に保存する。"""

    return Store(Path(directory) / DB_NAME) if directory else Store()
