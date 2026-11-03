"""計画の表示(表形式)とy/n確認。"""
from __future__ import annotations

from .plan import Plan, PlanStep


def _format_step(step: PlanStep) -> list[str]:
    parts = [f"#{step.index}", step.type, step.target or step.text or ""]

    if step.mouse:
        parts.append(
            f"move(duration_jitter={step.mouse.duration_jitter:.2f}s, "
            f"overshoot={step.mouse.overshoot}, "
            f"pre={step.mouse.pre_wait:.2f}s, post={step.mouse.post_wait:.2f}s)"
        )
    if step.click:
        parts.append(
            f"click(range={step.click.ratio:.0%}, down_up={step.click.mouse_down_up_ms:.0f}ms)"
        )
    if step.typing:
        parts.append(
            f"type(wpm={step.typing.wpm:.0f}, "
            f"mistakes={len(step.typing.mistakes)}, "
            f"pauses={len(step.typing.think_pause_positions)})"
        )

    parts.append(f"wait_after={step.wait_after:.2f}s")
    return parts


def render_table(plan: Plan) -> str:
    """人が読める表形式でPlanを表示する。"""
    lines = [f"Plan (seed={plan.seed}, steps={len(plan.steps)})", "-" * 60]

    for step in plan.steps:
        lines.append(" | ".join(_format_step(step)))

        if step.type == "click":
            lines.append("    ※クリック座標は実行直前のスクリーンショットで要素認識後に確定します")

        if step.typing and step.typing.mistakes:
            for m in step.typing.mistakes:
                lines.append(
                    f"    - mistake@{m.position}: {m.mistake_type.value} "
                    f"(notice_delay={m.notice_delay_chars}文字)"
                )

    return "\n".join(lines)


def confirm(plan: Plan) -> bool:
    """y/nでユーザーに実行確認を求める。承認された場合のみ、同じ計画とシードで実行される。"""
    print(render_table(plan))
    answer = input("この計画を実行しますか？ [y/N]: ").strip().lower()
    return answer == "y"
