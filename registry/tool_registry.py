from agent.models import Tool


class ToolRegistry:

    def __init__(self):

        self.tools = {}

    def register(self, tool: Tool):

        self.tools[tool.name] = tool

    def get(self, name):

        return self.tools.get(name)

    def list(self):

        return list(self.tools.values())