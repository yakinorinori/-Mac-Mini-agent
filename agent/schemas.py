"""LLMの出力とツール引数の検証。不正なものは実行前に拒否する。"""
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from agent.models import Plan, Step

MAX_PLAN_STEPS = 5
MAX_DELAY_SECONDS = 24 * 60 * 60

SingleLine = Annotated[str, StringConstraints(pattern=r"^[^\r\n]*$")]
# "/" を含むパスや "-" で始まるオプションをアプリ名として受け付けない
AppName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[^/\\\r\n-][^/\\\r\n]*$"),
]


class PlanValidationError(ValueError):
    """LLMの出力やツール引数が、実行してよい形式ではない。"""


class ToolArgs(BaseModel):
    """ツール引数の基底クラス。未知の引数と型の自動変換を認めない。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class NoArgs(ToolArgs):
    pass


class _StepSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool: str = Field(min_length=1, max_length=100)
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=1000)


class _PlanSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    steps: list[_StepSchema] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    message: str = Field(default="", max_length=2000)
    # 0 はLLMが「遅延なし」の意味で返すことがあるので認める
    delay: int | None = Field(default=None, ge=0, le=MAX_DELAY_SECONDS)


def _summarize(error: ValidationError) -> str:
    """入力値(メール本文など)を含めず、問題のある項目と種類だけを返す。"""

    problems = []

    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in item["loc"]) or "全体"
        problems.append(f"{location}({item['type']})")

    return "、".join(problems)


def validate_tool_args(tool, args) -> dict[str, Any]:
    """ツール引数を検証し、関数へ渡せる辞書を返す。"""

    try:
        return tool.args_schema.model_validate(args).model_dump()
    except ValidationError as e:
        raise PlanValidationError(f"{tool.name} の引数が不正です: {_summarize(e)}") from e


def build_plan(data, registry) -> Plan:
    """LLMが返したJSONデータを検証し、実行可能なPlanにする。"""

    try:
        parsed = _PlanSchema.model_validate(data)
    except ValidationError as e:
        raise PlanValidationError(f"Planの形式が不正です: {_summarize(e)}") from e

    steps = []

    for step in parsed.steps:
        tool = registry.get(step.tool) if registry is not None else None

        if tool is None:
            raise PlanValidationError(f"登録されていないツールです: {step.tool}")

        steps.append(
            Step(
                tool=step.tool,
                args=validate_tool_args(tool, step.args),
                reason=step.reason,
            )
        )

    return Plan(steps=steps, message=parsed.message, delay=parsed.delay)
