from collections import deque
from typing import Optional

from agent.models import Plan

# 計画にはメール本文などの引数が入るので、メモリ上でも直近の分だけ持つ
HISTORY_LIMIT = 20


class Context:

    def __init__(self, history_limit=HISTORY_LIMIT):

        self.last_command: Optional[Plan] = None

        self.history = deque(maxlen=history_limit)

        self.dev_mode = False  # 開発モード中は発言をそのままVS Codeのチャットへ送る

    def add(self, text: str, command: Plan):

        self.last_command = command

        self.history.append({
            "text": text,
            "command": command
        })

    def get_last_command(self):

        return self.last_command

    def get_history(self):

        return list(self.history)
