from agent.models import Tool
from .open_app import open_app
from .close_app import close_app


def register(registry):

    registry.register(
        Tool(
            name="open_app",
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