from agent.models import Intent


class Parser:

    def parse(self, text: str) -> Intent:

        return Intent(
            text=text.strip()
        )