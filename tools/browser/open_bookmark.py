import subprocess
from config import BOOKMARKS


def open_bookmark(name: str):
    """ブックマーク名からURLを解決してChromeで開く"""

    # 大文字小文字・空白を無視して検索
    key = next(
        (k for k in BOOKMARKS if k.lower() == name.lower()),
        None
    )

    if key is None:
        print(f"[open_bookmark] ブックマークが見つかりません: {name}")
        print(f"[open_bookmark] 利用可能: {list(BOOKMARKS.keys())}")
        return

    url = BOOKMARKS[key]
    print(f"[open_bookmark] {key} → {url}")

    subprocess.run(["open", "-a", "Google Chrome", url])
