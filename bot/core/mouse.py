"""人間らしいマウス移動(HumanMouse)。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

Point = tuple[float, float]


@dataclass(frozen=True)
class MoveParams:
    """乱数で確定済みの移動パラメータ(Plan生成時に決定し、実行時はこれをそのまま使う)。

    Plan生成時点では実座標(距離)が未確定なため、基準時間への加算分(jitter)のみを持つ。
    実際の移動時間は実行時に compute_duration() で距離から算出する。
    """
    duration_jitter: float
    overshoot: bool
    overshoot_extra_px: float
    pre_wait: float
    post_wait: float
    path_seed: int


def _distance(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def compute_move_params(config: dict, rng: random.Random) -> MoveParams:
    """移動時間のジッター・オーバーシュート有無などを乱数で決定する(距離非依存)。"""
    duration_jitter = rng.uniform(*config["duration_jitter_range"])

    overshoot = rng.random() < config["overshoot_probability"]
    overshoot_extra_px = rng.uniform(*config["overshoot_extra_px_range"]) if overshoot else 0.0

    pre_wait = rng.uniform(*config["pre_move_wait_range"])
    post_wait = rng.uniform(*config["post_move_wait_range"])
    path_seed = rng.randrange(2**31)

    return MoveParams(
        duration_jitter=duration_jitter,
        overshoot=overshoot,
        overshoot_extra_px=overshoot_extra_px,
        pre_wait=pre_wait,
        post_wait=post_wait,
        path_seed=path_seed,
    )


def compute_duration(distance: float, config: dict, duration_jitter: float) -> float:
    """距離が長いほど基準時間が長くなるよう、距離とジッターから移動時間を算出する。"""
    base = (distance / 1000.0) * config["duration_base_per_1000px"]
    return max(0.05, base + duration_jitter)


def _ease_in_out(t: float) -> float:
    """加速→減速のイージング(距離が長いほど速く、目標付近で減速)。"""
    return t * t * (3 - 2 * t)


def generate_bezier_path(start: Point, end: Point, params: MoveParams, config: dict) -> list[Point]:
    """ベジェ曲線(制御点ランダム)+ジッターの軌跡を生成する。path_seedで再現可能。"""
    rng = random.Random(params.path_seed)

    dx, dy = end[0] - start[0], end[1] - start[1]
    length = max(1.0, (dx ** 2 + dy ** 2) ** 0.5)
    nx, ny = -dy / length, dx / length  # 進行方向に垂直なベクトル

    control_jitter_min, control_jitter_max = config["bezier_control_jitter_px"]

    def rand_offset() -> float:
        return rng.choice([-1, 1]) * rng.uniform(control_jitter_min, control_jitter_max)

    p0 = start
    p1 = (start[0] + dx * 0.33 + nx * rand_offset(), start[1] + dy * 0.33 + ny * rand_offset())
    p2 = (start[0] + dx * 0.66 + nx * rand_offset(), start[1] + dy * 0.66 + ny * rand_offset())

    final_end = end
    if params.overshoot:
        final_end = (
            end[0] + (dx / length) * params.overshoot_extra_px,
            end[1] + (dy / length) * params.overshoot_extra_px,
        )
    p3 = final_end

    n_points = config.get("path_points", 24)
    jitter_min, jitter_max = config["path_jitter_px"]

    points: list[Point] = []
    for i in range(n_points + 1):
        t = _ease_in_out(i / n_points)
        x = (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1]
        jx = rng.uniform(jitter_min, jitter_max) * rng.choice([-1, 1])
        jy = rng.uniform(jitter_min, jitter_max) * rng.choice([-1, 1])
        points.append((x + jx, y + jy))

    if params.overshoot:
        # オーバーシュート後、目標に戻る短い区間を追加する
        back_steps = max(3, n_points // 6)
        for i in range(1, back_steps + 1):
            t = i / back_steps
            points.append((
                final_end[0] + (end[0] - final_end[0]) * t,
                final_end[1] + (end[1] - final_end[1]) * t,
            ))

    return points


class HumanMouse:
    """Quartz CGEventで実際にマウスを動かす実行部。乱数はMoveParamsとして受け取り、実行時に再抽選しない。"""

    def move(self, start: Point, end: Point, params: MoveParams, config: dict) -> None:
        distance = _distance(start, end)
        duration = compute_duration(distance, config, params.duration_jitter)
        path = generate_bezier_path(start, end, params, config)
        self._move_along_path(path, duration, params.pre_wait, params.post_wait)

    def _move_along_path(self, path: list[Point], duration: float, pre_wait: float, post_wait: float) -> None:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventMouseMoved,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )

        time.sleep(pre_wait)

        step_delay = duration / max(1, len(path))
        for point in path:
            event = CGEventCreateMouseEvent(None, kCGEventMouseMoved, point, kCGMouseButtonLeft)
            CGEventPost(kCGHIDEventTap, event)
            time.sleep(step_delay)

        time.sleep(post_wait)
