from tool_manager import ToolManager
from agent.models import Command

tm = ToolManager()

command = Command(
    tool="open_app",
    args={
        "name":"Notes"
    },
    message="メモを開きました"
)

tm.execute(command)