"""スクリーンキャプチャとRetinaスケール補正。"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScreenInfo:
    logical_width: int
    logical_height: int
    pixel_width: int
    pixel_height: int

    @property
    def scale_x(self) -> float:
        return self.pixel_width / self.logical_width

    @property
    def scale_y(self) -> float:
        return self.pixel_height / self.logical_height


def get_screen_info() -> ScreenInfo:
    """Quartz/AppKitから論理座標とピクセル座標を取得する(Retina対応)。"""
    from Quartz import CGMainDisplayID, CGDisplayPixelsWide, CGDisplayPixelsHigh
    from AppKit import NSScreen

    display_id = CGMainDisplayID()
    pixel_width = CGDisplayPixelsWide(display_id)
    pixel_height = CGDisplayPixelsHigh(display_id)

    screen = NSScreen.mainScreen()
    logical_size = screen.frame().size

    return ScreenInfo(
        logical_width=int(logical_size.width),
        logical_height=int(logical_size.height),
        pixel_width=int(pixel_width),
        pixel_height=int(pixel_height),
    )


def capture_full_screen(output_path: Path, screencapture_path: str = "/usr/sbin/screencapture") -> Path:
    """screencaptureコマンドで全画面をキャプチャする(ピクセル座標の画像が得られる)。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([screencapture_path, "-x", str(output_path)], check=True)
    return output_path


def logical_to_pixel(x: float, y: float, info: ScreenInfo) -> tuple[float, float]:
    return x * info.scale_x, y * info.scale_y


def pixel_to_logical(x: float, y: float, info: ScreenInfo) -> tuple[float, float]:
    return x / info.scale_x, y / info.scale_y


def check_permissions() -> None:
    """画面収録・アクセシビリティ権限を確認し、未許可なら分かりやすく例外を出す。"""
    from Quartz import CGPreflightScreenCaptureAccess
    from ApplicationServices import AXIsProcessTrusted

    problems = []
    if not CGPreflightScreenCaptureAccess():
        problems.append(
            "画面収録の権限がありません。"
            "システム設定 > プライバシーとセキュリティ > 画面収録 で許可してください。"
        )
    if not AXIsProcessTrusted():
        problems.append(
            "アクセシビリティの権限がありません。"
            "システム設定 > プライバシーとセキュリティ > アクセシビリティ で許可してください。"
        )
    if problems:
        raise PermissionError("\n".join(problems))
