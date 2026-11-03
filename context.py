from typing import Optional
from agent.models import Plan
from memory import Memory


class Context:

    def __init__(self):

        self.last_command: Optional[Plan] = None

        self.history = []

        self.memory = Memory()

        self.dev_mode = False  # 開発モード中は発言をそのままVS Codeのチャットへ送る

    def add(self, text: str, command: Plan):

        self.last_command = command

        self.history.append({
            "text": text,
            "command": command
        })

        self.memory.add(text, command)

    def get_last_command(self):

        return self.last_command

    def get_history(self):

        return self.history