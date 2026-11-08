from agent.parser import Parser
from agent.planner import Planner


class Agent:

    def __init__(self, context, registry=None, rule_lookup=None):

        self.parser = Parser()

        self.planner = Planner(context, registry=registry, rule_lookup=rule_lookup)

    def plan(self, text):

        intent = self.parser.parse(text)

        return self.planner.plan(intent)
