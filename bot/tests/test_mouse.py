import random

from core.mouse import MoveParams, compute_move_params, generate_bezier_path

CONFIG = {
    "duration_base_per_1000px": 0.5,
    "duration_jitter_range": [0.3, 1.2],
    "bezier_control_jitter_px": [10, 60],
    "path_jitter_px": [0.5, 2.5],
    "overshoot_probability": 0.5,
    "overshoot_extra_px_range": [8, 25],
    "pre_move_wait_range": [0.05, 0.4],
    "post_move_wait_range": [0.05, 0.4],
    "path_points": 24,
}


def test_compute_move_params_within_config_ranges():
    rng = random.Random(1)
    params = compute_move_params(CONFIG, rng)

    assert CONFIG["duration_jitter_range"][0] <= params.duration_jitter <= CONFIG["duration_jitter_range"][1]
    assert CONFIG["pre_move_wait_range"][0] <= params.pre_wait <= CONFIG["pre_move_wait_range"][1]
    assert CONFIG["post_move_wait_range"][0] <= params.post_wait <= CONFIG["post_move_wait_range"][1]


def test_same_seed_produces_identical_path():
    params = MoveParams(0.5, False, 0.0, 0.1, 0.1, path_seed=42)

    path1 = generate_bezier_path((0, 0), (500, 300), params, CONFIG)
    path2 = generate_bezier_path((0, 0), (500, 300), params, CONFIG)

    assert path1 == path2


def test_different_seed_produces_different_path():
    params1 = MoveParams(0.5, False, 0.0, 0.1, 0.1, path_seed=1)
    params2 = MoveParams(0.5, False, 0.0, 0.1, 0.1, path_seed=2)

    path1 = generate_bezier_path((0, 0), (500, 300), params1, CONFIG)
    path2 = generate_bezier_path((0, 0), (500, 300), params2, CONFIG)

    assert path1 != path2


def test_overshoot_adds_points_beyond_target():
    params = MoveParams(0.5, True, 20.0, 0.1, 0.1, path_seed=7)
    path = generate_bezier_path((0, 0), (500, 0), params, CONFIG)

    assert any(x > 500 for x, _ in path)


def test_many_seeds_produce_varied_paths():
    endpoints = set()
    for seed in range(20):
        params = MoveParams(0.5, False, 0.0, 0.1, 0.1, path_seed=seed)
        path = generate_bezier_path((0, 0), (500, 300), params, CONFIG)
        endpoints.add(tuple(round(v, 2) for v in path[-1]))

    assert len(endpoints) > 1
