from agent.process import run_command
from config import BOOKMARKS


def open_bookmark(name: str):
    """ブックマーク名からURLを解決してChromeで開く"""

    # 大文字小文字・空白を無視して検索
    key = next(
        (k for k in BOOKMARKS if k.lower() == name.lower()),
        None
    )

    if key is None:
        raise ValueError(
            f"ブックマークが見つかりません: {name}(利用可能: {', '.join(BOOKMARKS)})"
        )

    url = BOOKMARKS[key]
    print(f"[open_bookmark] {key} → {url}")

    run_command(["open", "-a", "Google Chrome", url])
