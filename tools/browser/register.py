from agent.models import Tool
from .google_search import google_search
from .open_url import open_url
from .open_bookmark import open_bookmark
from config import BOOKMARKS


def register(registry):

    registry.register(
        Tool(
            name="google_search",
            function=google_search,
            description="""
GoogleでWeb検索を行う。

引数:
query

例
今日の天気
Python チュートリアル
最新ニュース
"""
        )
    )

    registry.register(
        Tool(
            name="open_url",
            function=open_url,
            description="""
指定したURLをブラウザで開く。

引数:
url

例
https://www.google.com
https://github.com
"""
        )
    )

    # ブックマーク一覧を動的にdescriptionに反映
    bookmark_list = "\n".join(f"  {k}" for k in BOOKMARKS.keys())

    registry.register(
        Tool(
            name="open_bookmark",
            function=open_bookmark,
            description=f"""
登録済みのブックマークをChromeで開く。

引数:
name  ← 以下のブックマーク名を指定

登録済みブックマーク:
{bookmark_list}
"""
        )
    )
