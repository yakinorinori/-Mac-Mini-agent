from tool_manager import ToolManager

tm = ToolManager()

for tool in tm.list_tools():

    print(tool.name)
    print(tool.description)