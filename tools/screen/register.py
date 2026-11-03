from agent.models import Tool
from .take_screenshot import take_screenshot


def register(registry):

    registry.register(
        Tool(
            name="take_screenshot",
            function=take_screenshot,
            description="""
Macの画面全体のスクリーンショットを撮影する。
実行結果として画像ファイルのパスを返し、そのままTelegramへ画像として送信される。

引数:
なし

例
画面のスクリーンショットを送って
今の画面を見せて
今の画面どうなってる？
"""
        )
    )
