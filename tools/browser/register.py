from pydantic import Field

from agent.models import Tool
from agent.schemas import ToolArgs
from .google_search import google_search
from .open_url import open_url
from .open_bookmark import open_bookmark
from config import BOOKMARKS


class SearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=500)


class UrlArgs(ToolArgs):
    url: str = Field(min_length=1, max_length=2048)


class BookmarkArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=100)


def register(registry):

    registry.register(
        Tool(
            name="google_search",
            requires_approval=True,
            args_schema=SearchArgs,
            function=google_search,
            description="""
Googleの検索結果をブラウザで開き、URLを返す。結果の読み取りや要約はしない。
ユーザーがWeb検索を求めている時だけ選ぶ。挨拶や雑談では選ばない。

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
            args_schema=UrlArgs,
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
            args_schema=BookmarkArgs,
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
