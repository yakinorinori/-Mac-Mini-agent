from agent.models import Tool
from .mail_send import mail_send


def register(registry):

    registry.register(
        Tool(
            name="mail_send",
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
