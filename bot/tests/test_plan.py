from planner.plan import Action, build_plan

CONFIG = {
    "mouse": {
        "duration_base_per_1000px": 0.5,
        "duration_jitter_range": [0.3, 1.2],
        "bezier_control_jitter_px": [10, 60],
        "path_jitter_px": [0.5, 2.5],
        "overshoot_probability": 0.1,
        "overshoot_extra_px_range": [8, 25],
        "pre_move_wait_range": [0.05, 0.4],
        "post_move_wait_range": [0.05, 0.4],
        "path_points": 24,
    },
    "click": {
        "bbox_ratio_range": [0.4, 0.8],
        "inner_margin_ratio": 0.1,
        "gaussian_sigma_ratio": 0.35,
        "mouse_down_up_ms_range": [50, 160],
        "double_click_interval_ms_range": [80, 220],
    },
    "typing": {
        "base_wpm": 220,
        "wpm_jitter_ratio": 0.25,
        "think_pause_probability": 0.1,
        "think_pause_range": [0.4, 1.6],
        "word_boundary_extra_range": [0.05, 0.25],
        "punctuation_extra_range": [0.1, 0.4],
        "ime_commit_pre_wait_range": [0.1, 0.3],
        "ime_commit_post_wait_range": [0.1, 0.35],
        "mistake": {
            "notice_delay_chars_range": [1, 3],
            "types": ["adjacent_key", "swap", "omission"],
        },
    },
    "planner": {
        "step_wait_range": [0.1, 0.5],
    },
}


def test_build_plan_same_seed_is_reproducible():
    actions = [Action(type="click", target="ボタンA"), Action(type="type", text="てすと", mistakes=1)]

    plan1 = build_plan(actions, CONFIG, seed=123)
    plan2 = build_plan(actions, CONFIG, seed=123)

    assert plan1.to_dict()["steps"] == plan2.to_dict()["steps"]


def test_build_plan_click_step_has_mouse_and_click_params():
    plan = build_plan([Action(type="click", target="ボタンA")], CONFIG, seed=1)
    step = plan.steps[0]

    assert step.mouse is not None
    assert step.click is not None
    assert step.typing is None


def test_build_plan_type_step_has_typing_params():
    plan = build_plan([Action(type="type", text="あいうえお", mistakes=2)], CONFIG, seed=1)
    step = plan.steps[0]

    assert step.typing is not None
    assert len(step.typing.mistakes) == 2


def test_different_seed_gives_different_plan():
    actions = [Action(type="click", target="ボタンA")]

    plan1 = build_plan(actions, CONFIG, seed=1)
    plan2 = build_plan(actions, CONFIG, seed=2)

    assert plan1.to_dict()["steps"] != plan2.to_dict()["steps"]
