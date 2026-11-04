"""受け付けた処理を1件ずつ順に実行するキュー。

メモリの少ないMacでLLMを同時に動かさず、macOSの操作が重ならないようにする。
ワーカーは処理がある間だけ動き、イベントループを止めない。
"""
import asyncio
from collections import deque
from dataclasses import dataclass


@dataclass
class Job:
    label: str
    factory: object
    owner: int


class JobQueue:

    def __init__(self):
        self._pending = deque()
        self._running = None
        self._worker = None

    def submit(self, label, factory, owner) -> int:
        """処理を追加し、先に控えている件数を返す。factoryはコルーチンを返す関数。"""

        ahead = len(self._pending) + (1 if self._running else 0)
        self._pending.append(Job(label, factory, owner))

        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

        return ahead

    def cancel_pending(self, owner) -> list[str]:
        """本人の未開始の処理を取り消し、取り消した処理の説明を返す。"""

        cancelled = [job for job in self._pending if job.owner == owner]
        self._pending = deque(job for job in self._pending if job.owner != owner)

        return [job.label for job in cancelled]

    def running_label(self, owner):
        return self._running.label if self._running and self._running.owner == owner else None

    async def join(self):
        while self._worker is not None and not self._worker.done():
            await asyncio.wait({self._worker})

    def close(self):
        self._pending.clear()

        if self._worker is not None:
            self._worker.cancel()

    async def _work(self):
        while self._pending:
            self._running = self._pending.popleft()

            try:
                await self._running.factory()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 1件の失敗で後続を止めない。内容を含めないよう種類だけ記録する
                print("ジョブエラー:", type(e).__name__)
            finally:
                self._running = None
