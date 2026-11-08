"""Telegramの音声メモをMac内で文字起こしする。

whisper.cpp(`whisper-cli`)とffmpegを外部コマンドとして呼ぶ。処理ごとにプロセスが終わるので、
音声認識のメモリはLLMと同時に使われ続けない(ジョブキューで1件ずつ実行する)。
音声と文字起こしは一時フォルダに置き、終わったら消す。外部へは送らない。

導入(Mac mini):
    brew install whisper-cpp ffmpeg
    mkdir -p ~/agent/models
    curl -L -o ~/agent/models/ggml-large-v3-turbo-q5_0.bin \
        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
    python -m voice --check
    python -m voice 録音ファイル     # 所要時間の計測
"""
import os
import shutil
import sys
import time
from pathlib import Path

from agent.process import run_command

DEFAULT_MODEL_NAME = "ggml-large-v3-turbo-q5_0.bin"
LANGUAGE = "ja"

# 2分の音声でも、M1で処理しきれる余裕を持たせる(実測後に調整する)
TRANSCRIBE_TIMEOUT_SECONDS = 300
CONVERT_TIMEOUT_SECONDS = 60

# 無音や雑音に対してWhisperが出しやすい定型文。これだけなら聞き取れなかったとみなす
HALLUCINATIONS = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "チャンネル登録よろしくお願いします",
    "チャンネル登録よろしくお願いします。",
)


def whisper_cli():
    return os.getenv("WHISPER_CLI") or shutil.which("whisper-cli")


def ffmpeg():
    return os.getenv("FFMPEG") or shutil.which("ffmpeg")


def model_path() -> Path:
    value = os.getenv("WHISPER_MODEL")
    return Path(value) if value else Path.home() / "agent" / "models" / DEFAULT_MODEL_NAME


def setup_problems() -> list[str]:
    """足りないものを返す。空なら文字起こしできる。"""

    problems = []

    if not whisper_cli():
        problems.append("whisper-cli（brew install whisper-cpp）")
    if not ffmpeg():
        problems.append("ffmpeg（brew install ffmpeg）")
    if not model_path().is_file():
        problems.append(f"モデル {model_path()}")

    return problems


def clean_transcript(raw: str) -> str:
    """行をつなぎ、Whisperの定型の誤認識だけなら空にする。"""

    lines = [line.strip() for line in raw.splitlines()]
    lines = [line for line in lines if line and line not in HALLUCINATIONS]

    return "".join(lines).strip()


def transcribe(audio: Path, workdir: Path) -> str:
    """音声ファイルを文字起こしする。workdir に中間ファイルを書く(呼び出し側で消す)。"""

    wav = workdir / "voice.wav"
    base = workdir / "transcript"

    # Telegramの音声(Opus)をWhisperの入力形式(16kHz、モノラル)へ変換する
    run_command(
        [ffmpeg(), "-nostdin", "-loglevel", "error", "-y",
         "-i", str(audio), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        timeout=CONVERT_TIMEOUT_SECONDS,
    )

    run_command(
        [whisper_cli(), "-m", str(model_path()), "-f", str(wav), "-l", LANGUAGE,
         "-nt", "-np", "-otxt", "-of", str(base)],
        timeout=TRANSCRIBE_TIMEOUT_SECONDS,
    )

    return clean_transcript(base.with_suffix(".txt").read_text(encoding="utf-8"))


def _main(argv):
    problems = setup_problems()

    if argv[:1] == ["--check"] or not argv:
        if problems:
            print("未導入: " + "、".join(problems))
            return 1
        print(f"準備OK: {whisper_cli()}、{ffmpeg()}、{model_path()}")
        return 0

    if problems:
        print("未導入: " + "、".join(problems))
        return 1

    import tempfile

    with tempfile.TemporaryDirectory(prefix="agent-voice-") as directory:
        started = time.monotonic()
        text = transcribe(Path(argv[0]), Path(directory))
        print(f"{time.monotonic() - started:.1f}秒: {text or '（聞き取れませんでした）'}")

    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
