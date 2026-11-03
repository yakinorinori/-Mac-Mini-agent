import random

from core.click import compute_click_params, compute_click_point
from core.vision import BoundingBox

CONFIG = {
    "bbox_ratio_range": [0.4, 0.8],
    "inner_margin_ratio": 0.1,
    "gaussian_sigma_ratio": 0.35,
    "mouse_down_up_ms_range": [50, 160],
    "double_click_interval_ms_range": [80, 220],
}


def test_compute_click_params_within_ranges():
    rng = random.Random(1)
    params = compute_click_params(CONFIG, rng)

    assert CONFIG["bbox_ratio_range"][0] <= params.ratio <= CONFIG["bbox_ratio_range"][1]
    assert CONFIG["mouse_down_up_ms_range"][0] <= params.mouse_down_up_ms <= CONFIG["mouse_down_up_ms_range"][1]


def test_click_point_stays_within_box_bounds():
    box = BoundingBox(x=100, y=100, width=200, height=100)
    rng = random.Random(2)

    for _ in range(200):
        params = compute_click_params(CONFIG, rng)
        x, y = compute_click_point(box, params, CONFIG)
        assert box.x <= x <= box.x + box.width
        assert box.y <= y <= box.y + box.height


def test_click_point_differs_across_calls():
    box = BoundingBox(x=0, y=0, width=300, height=200)
    rng = random.Random(3)

    points = {compute_click_point(box, compute_click_params(CONFIG, rng), CONFIG) for _ in range(20)}

    assert len(points) > 1
