from datetime import datetime
from pathlib import Path

from agent.process import CommandError, run_command


def take_screenshot():
    """画面全体のスクリーンショットを撮影し、保存先のファイルパスを返す"""

    output_dir = Path(__file__).resolve().parent / "captures"
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = output_dir / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

    run_command(["screencapture", "-x", str(filename)])

    if not filename.is_file():
        raise CommandError("screencapture が画像を作成しませんでした")

    return str(filename)
