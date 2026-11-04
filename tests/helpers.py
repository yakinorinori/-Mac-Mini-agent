"""テスト用の補助関数。"""
import asyncio


def run_and_drain(coroutine):
    """ハンドラを実行し、キューに入った処理の完了まで待つ。"""
    import main

    async def scenario():
        await coroutine
        await main.jobs.join()

    asyncio.run(scenario())
