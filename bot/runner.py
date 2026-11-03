"""Plan → Confirm → Execute のCLIエントリポイント。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

from core.click import HumanClick, compute_click_point
from core.keyboard import HumanTyping
from core.mouse import HumanMouse
from core.screen import capture_full_screen, check_permissions
from core.vision import ClaudeVisionRecognizer, DummyRecognizer, TemplateMatchRecognizer, locate_with_retry
from planner.confirm import confirm, render_table
from planner.plan import Action, build_plan, replan
from safety import EmergencyStop, SafetyMonitor, check_expected_app, released_inputs_on_exit

BASE_DIR = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def build_recognizer(config: dict):
    provider = config["vision"]["provider"]

    if provider == "claude":
        api_key = os.environ.get(config["vision"]["claude"]["api_key_env"])
        if not api_key:
            print("[Vision] APIキー未設定のためDummyRecognizerで動作します")
            return DummyRecognizer()
        return ClaudeVisionRecognizer(api_key=api_key, model=config["vision"]["claude"]["model"])

    if provider == "template":
        return TemplateMatchRecognizer(templates={}, threshold=config["vision"]["template"]["match_threshold"])

    return DummyRecognizer()


def log_event(log_path: Path, event: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), **event}, ensure_ascii=False) + "\n")


def execute_plan(plan, config: dict, recognizer, safety: SafetyMonitor, log_path: Path) -> None:
    mouse = HumanMouse()
    click = HumanClick()
    typing = HumanTyping()

    current_pos = (0.0, 0.0)  # 実機では現在のカーソル位置取得APIに置き換える

    for step in plan.steps:
        if safety.triggered():
            raise EmergencyStop("緊急停止が発火しました")

        log_event(log_path, {"event": "step_start", "step": step.index, "type": step.type})

        if step.type == "click":
            screen_path = capture_full_screen(BASE_DIR / "debug" / f"step{step.index}.png")
            box = locate_with_retry(
                recognizer,
                lambda: screen_path,
                step.target,
                max_retries=config["vision"]["claude"]["max_retries"],
                retry_wait_range=tuple(config["vision"]["claude"]["retry_wait_range"]),
                min_confidence=config["vision"]["claude"]["min_confidence"],
            )
            if box is None:
                log_event(log_path, {"event": "not_found", "step": step.index, "target": step.target})
                raise RuntimeError(f"要素が見つかりません: {step.target}")

            point = compute_click_point(box, step.click, config["click"])

            mouse.move(current_pos, point, step.mouse, config["mouse"])
            click.click(point, step.click)

            log_event(log_path, {"event": "click", "step": step.index, "ratio": step.click.ratio, "point": point})
            current_pos = point

        elif step.type == "type":
            typing.type_text(step.text or "", step.typing, config["typing"])
            log_event(log_path, {"event": "type", "step": step.index, "mistakes": len(step.typing.mistakes)})

        elif step.type == "wait":
            time.sleep(step.wait_after)

        time.sleep(step.wait_after)
        log_event(log_path, {"event": "step_end", "step": step.index})


def main() -> None:
    parser = argparse.ArgumentParser(description="人間らしい自動操作Bot")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="計画表示のみ")
    parser.add_argument("--replan", action="store_true", help="乱数だけ再抽選する")
    parser.add_argument("--expected-app", default=None, help="実行前に確認するアクティブアプリ名")
    args = parser.parse_args()

    config = load_config(Path(args.config))

    check_permissions()

    if args.expected_app and config["safety"]["expected_app_check"]:
        if not check_expected_app(args.expected_app):
            print(f"想定アプリ({args.expected_app})がアクティブではないため中止します")
            sys.exit(1)

    # サンプルフロー: TextEditへの入力デモ
    actions = [
        Action(type="click", target="TextEditの本文エリア"),
        Action(type="type", text="こんにちは、これはテストです。", mistakes=2),
    ]

    plan = replan(actions, config) if args.replan else build_plan(actions, config)

    if args.dry_run:
        print(render_table(plan))
        return

    plan_path = Path(config["planner"]["plan_json_dir"]) / f"plan_{plan.seed}.json"
    plan.save(plan_path)

    if not confirm(plan):
        print("中止しました")
        return

    recognizer = build_recognizer(config)
    safety = SafetyMonitor(
        esc_long_press_seconds=config["safety"]["esc_long_press_seconds"],
        corner_margin_px=config["safety"]["corner_abort_margin_px"],
    )
    safety.start()

    log_path = BASE_DIR / "logs" / f"run_{plan.seed}.jsonl"

    with released_inputs_on_exit():
        execute_plan(plan, config, recognizer, safety, log_path)

    safety.stop()
    print("実行完了")


if __name__ == "__main__":
    main()
