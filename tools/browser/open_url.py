from urllib.parse import urlparse

from agent.process import run_command

_ALLOWED_SCHEMES = {"http", "https"}


def open_url(url):

    parsed = urlparse(url)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise ValueError("開けるのは http:// または https:// のURLだけです")

    run_command(["open", url])
