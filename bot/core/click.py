"""人間らしいクリック(HumanClick)。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from .vision import BoundingBox


@dataclass(frozen=True)
class ClickParams:
    """乱数で確定済みのクリックパラメータ。"""
    ratio: float                  # 要素の幅高さに対する許容範囲の割合
    offset_seed: int              # ガウス分布による座標決定の乱数シード
    mouse_down_up_ms: float
    double_click_interval_ms: float


def compute_click_params(config: dict, rng: random.Random) -> ClickParams:
    """許容範囲の割合・座標決定シード・押下時間などを乱数で決定する。"""
    return ClickParams(
        ratio=rng.uniform(*config["bbox_ratio_range"]),
        offset_seed=rng.randrange(2**31),
        mouse_down_up_ms=rng.uniform(*config["mouse_down_up_ms_range"]),
        double_click_interval_ms=rng.uniform(*config["double_click_interval_ms_range"]),
    )


def compute_click_point(box: BoundingBox, params: ClickParams, config: dict) -> tuple[float, float]:
    """要素の許容範囲内でガウス分布(中心寄り)により座標を決定する。毎回異なる座標になる。"""
    rng = random.Random(params.offset_seed)

    margin_ratio = config["inner_margin_ratio"]
    usable_w = box.width * (1 - 2 * margin_ratio)
    usable_h = box.height * (1 - 2 * margin_ratio)

    allowed_w = usable_w * params.ratio
    allowed_h = usable_h * params.ratio

    cx, cy = box.center
    sigma_ratio = config["gaussian_sigma_ratio"]

    def clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    dx = clamp(rng.gauss(0, allowed_w * sigma_ratio), -allowed_w / 2, allowed_w / 2)
    dy = clamp(rng.gauss(0, allowed_h * sigma_ratio), -allowed_h / 2, allowed_h / 2)

    return cx + dx, cy + dy


class HumanClick:
    """Quartz CGEventで実際にクリックする実行部。"""

    def click(self, point: tuple[float, float], params: ClickParams, double_click: bool = False) -> None:
        self._press_release(point, params.mouse_down_up_ms)

        if double_click:
            time.sleep(params.double_click_interval_ms / 1000.0)
            self._press_release(point, params.mouse_down_up_ms)

    def _press_release(self, point: tuple[float, float], down_up_ms: float) -> None:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )

        down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, down)

        time.sleep(down_up_ms / 1000.0)

        up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, up)
