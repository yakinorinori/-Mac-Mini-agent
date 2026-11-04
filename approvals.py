"""実行前の承認待ちPlanを、一度だけ取り出せる形で保持する。"""
import secrets
import time
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 10 * 60


@dataclass
class PendingApproval:
    plan: object
    text: str
    user_id: int
    expires_at: float


class ApprovalStore:

    def __init__(self, ttl_seconds=DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._pending = {}

    def add(self, plan, text, user_id) -> str:
        self._remove_expired()
        approval_id = secrets.token_urlsafe(16)
        self._pending[approval_id] = PendingApproval(
            plan=plan,
            text=text,
            user_id=user_id,
            expires_at=self._clock() + self._ttl_seconds,
        )
        return approval_id

    def take(self, approval_id, user_id):
        """本人の有効な承認待ちを取り出す。二度目以降や期限切れは None を返す。"""

        pending = self._pending.get(approval_id)

        if pending is None or pending.user_id != user_id:
            return None

        del self._pending[approval_id]

        if self._clock() >= pending.expires_at:
            return None

        return pending

    def _remove_expired(self):
        now = self._clock()
        self._pending = {
            approval_id: pending
            for approval_id, pending in self._pending.items()
            if now < pending.expires_at
        }
