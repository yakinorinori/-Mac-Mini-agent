import subprocess
from datetime import datetime
from pathlib import Path


def take_screenshot():
    """画面全体のスクリーンショットを撮影し、保存先のファイルパスを返す"""

    output_dir = Path("tools/screen/captures")
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = output_dir / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

    subprocess.run(["screencapture", "-x", str(filename)])

    return str(filename)
