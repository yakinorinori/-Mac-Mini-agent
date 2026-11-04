from urllib.parse import quote_plus

from agent.process import run_command


def google_search(query):
    """Googleの検索結果をブラウザで開き、スマホからも開けるようURLを返す。"""

    url = f"https://www.google.com/search?q={quote_plus(query)}"
    run_command(["open", url])

    return url
