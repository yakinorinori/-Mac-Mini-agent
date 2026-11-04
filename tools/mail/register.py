from pydantic import Field

from agent.models import Tool
from agent.schemas import SingleLine, ToolArgs
from .mail_send import mail_send


class MailSendArgs(ToolArgs):
    # 宛先は1件だけ受け付ける
    to: str = Field(max_length=254, pattern=r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
    subject: SingleLine = Field(min_length=1, max_length=300)
    body: str = Field(max_length=20000)


def register(registry):

    registry.register(
        Tool(
            name="mail_send",
            requires_approval=True,
            args_schema=MailSendArgs,
            function=mail_send,
            description="""
メールを送信する。

引数:
to      : 宛先メールアドレス
subject : 件名
body    : 本文

例
to="example@example.com", subject="件名", body="本文"
"""
        )
    )
