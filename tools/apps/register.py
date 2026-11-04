from agent.models import Tool
from agent.schemas import AppName, ToolArgs
from .open_app import open_app
from .close_app import close_app


class AppArgs(ToolArgs):
    name: AppName


def register(registry):

    registry.register(
        Tool(
            name="open_app",
            args_schema=AppArgs,
            function=open_app,
            description="""
Macのアプリケーションを起動する。

引数:
name

例
Notes
Google Chrome
Calendar
Visual Studio Code 2
"""
        )
    )

    registry.register(
        Tool(
            name="close_app",
            args_schema=AppArgs,
            function=close_app,
            description="""
Macのアプリケーションを終了する。

引数:
name

例
Notes
Google Chrome
Calendar
Visual Studio Code 2
"""
        )
    )