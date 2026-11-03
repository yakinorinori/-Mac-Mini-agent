from agent.models import Command


class Memory:

    def __init__(self):
        self.logs = []

    def add(self, text: str, command: Command):
        self.logs.append({
            "text": text,
            "command": command
        })

    def latest(self):
        if not self.logs:
            return None
        return self.logs[-1]

    def all(self):
        return self.logs