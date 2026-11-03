"""実行計画(Plan)の生成。乱数で決定済みの値をすべてステップに記録する。"""
from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from core.click import ClickParams, compute_click_params
from core.keyboard import TypingParams, compute_typing_params
from core.mouse import MoveParams, compute_move_params

StepType = Literal["click", "type", "wait", "scroll"]


@dataclass
class PlanStep:
    index: int
    type: StepType
    target: str                      # 要素名(clickの場合の説明文)
    mouse: Optional[MoveParams] = None
    click: Optional[ClickParams] = None
    typing: Optional[TypingParams] = None
    wait_after: float = 0.0
    text: Optional[str] = None       # typeステップの入力テキスト本体


@dataclass
class Plan:
    seed: int
    steps: list[PlanStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "created_at": self.created_at,
            "steps": [asdict(step) for step in self.steps],
        }

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        return path


@dataclass
class Action:
    """1アクションの入力仕様(要素名 or テキスト)。"""
    type: StepType
    target: str = ""
    text: str = ""
    mistakes: int = 0
    wait_seconds: float = 0.0


def build_plan(actions: list[Action], config: dict, seed: Optional[int] = None) -> Plan:
    """一連のActionから、乱数を確定させたPlanを生成する。

    座標はここでは決定しない(実行直前のスクショで要素認識後に確定する)。
    """
    seed = seed if seed is not None else random.SystemRandom().randrange(2**31)
    rng = random.Random(seed)

    steps: list[PlanStep] = []

    for i, action in enumerate(actions):
        mouse_params = None
        click_params = None
        typing_params = None

        if action.type == "click":
            mouse_params = compute_move_params(config["mouse"], rng)
            click_params = compute_click_params(config["click"], rng)
        elif action.type == "type":
            typing_params = compute_typing_params(action.text, action.mistakes, config["typing"], rng)

        wait_after = action.wait_seconds or rng.uniform(*config["planner"]["step_wait_range"])

        steps.append(
            PlanStep(
                index=i,
                type=action.type,
                target=action.target,
                mouse=mouse_params,
                click=click_params,
                typing=typing_params,
                wait_after=wait_after,
                text=action.text or None,
            )
        )

    return Plan(seed=seed, steps=steps)


def replan(actions: list[Action], config: dict) -> Plan:
    """乱数だけを再抽選して新しいPlanを作る(--replan用)。"""
    return build_plan(actions, config, seed=None)
