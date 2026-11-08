"""返答をMac内で読み上げ、Telegramの音声メッセージにする。

macOS標準の `say` で音声を作り、ffmpegでTelegramの音声メッセージの形式(OGG/Opus)へ変換する。
文章は一時フォルダに置き、終わったら消す。外部の読み上げサービスへは送らない。

    python -m speech --check
    python -m speech 読み上げる文章     # 所要時間の計測(voice.oggを作る)
"""
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from agent.process import run_command

# macOSに標準で入っている日本語の声。SAY_VOICE で変更できる
DEFAULT_VOICE = "Kyoko"

# 長い返答を読み上げると待ち時間とファイルが大きくなるので、ここで区切る
MAX_SPOKEN_CHARS = 300

SAY_TIMEOUT_SECONDS = 60
CONVERT_TIMEOUT_SECONDS = 60

_URL = re.compile(r"https?://\S+")


def say():
    return os.getenv("SAY") or shutil.which("say")


def ffmpeg():
    return os.getenv("FFMPEG") or shutil.which("ffmpeg")


def voice_name():
    return os.getenv("SAY_VOICE") or DEFAULT_VOICE


def setup_problems() -> list[str]:
    """足りないものを返す。空なら読み上げできる。"""

    problems = []

    if not say():
        problems.append("say（macOSのみ）")
    if not ffmpeg():
        problems.append("ffmpeg（brew install ffmpeg）")

    return problems


def speakable(text: str) -> str:
    """絵文字や記号を除き、URLを「リンク」に置き換え、長さを抑える。"""

    text = _URL.sub("リンク", text)
    kept = []

    for char in text:
        category = unicodedata.category(char)

        if category in ("So", "Cs", "Co") or char in "‍️":
            continue  # 絵文字など。読み上げると名前が読まれる
        kept.append(" " if char == "\n" else char)

    text = re.sub(r"\s+", " ", "".join(kept)).strip()

    if len(text) > MAX_SPOKEN_CHARS:
        text = text[:MAX_SPOKEN_CHARS].rstrip() + "。以下は文字で確認してください。"

    return text


def synthesize(text: str, workdir: Path) -> Path:
    """文章をOGG/Opusの音声にする。workdir に中間ファイルを書く(呼び出し側で消す)。"""

    source = workdir / "speech.txt"
    aiff = workdir / "speech.aiff"
    ogg = workdir / "speech.ogg"

    # 文章は引数ではなくファイルで渡す。"-" で始まる文をオプションと誤解させないため
    source.write_text(text, encoding="utf-8")
    run_command([say(), "-v", voice_name(), "-f", str(source), "-o", str(aiff)], timeout=SAY_TIMEOUT_SECONDS)
    run_command(
        [ffmpeg(), "-nostdin", "-loglevel", "error", "-y",
         "-i", str(aiff), "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(ogg)],
        timeout=CONVERT_TIMEOUT_SECONDS,
    )

    return ogg


def _main(argv):
    problems = setup_problems()

    if problems:
        print("未導入: " + "、".join(problems))
        return 1

    if not argv or argv[:1] == ["--check"]:
        print(f"準備OK: {say()}（声: {voice_name()}）、{ffmpeg()}")
        return 0

    import tempfile

    with tempfile.TemporaryDirectory(prefix="agent-speech-") as directory:
        started = time.monotonic()
        ogg = synthesize(speakable(" ".join(argv)), Path(directory))
        shutil.copy(ogg, "voice.ogg")
        print(f"{time.monotonic() - started:.1f}秒: voice.ogg")

    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
