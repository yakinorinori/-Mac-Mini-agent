"""LLM出力とツール引数の検証の回帰テスト(OS操作はモックする)。"""
import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
from agent.executor import ToolManager
from agent.models import Intent, Plan, Step
from agent.planner import Planner
from agent.schemas import (
    MAX_DELAY_SECONDS,
    MAX_PLAN_STEPS,
    PlanValidationError,
    build_plan,
)
from context import Context

MANAGER = ToolManager()
REGISTRY = MANAGER.registry
SECRET = "秘密の本文"

VALID_ARGS = {
    "open_app": {"name": "Google Chrome"},
    "close_app": {"name": "Visual Studio Code 2"},
    "google_search": {"query": "天気"},
    "open_url": {"url": "https://example.com"},
    "open_bookmark": {"name": "GitHub"},
    "calendar_add": {"title": "会議", "date": "2026/11/10", "time": "14:00"},
    "excel_open": {"path": "~/report.xlsx"},
    "mail_send": {"to": "user@example.com", "subject": '"テスト" -s 確認', "body": "1行目\n2行目"},
    "take_screenshot": {},
    "send_to_vscode_chat": {"message": "確認して"},
}


def _plan_data(tool, args, **extra):
    return {"steps": [{"tool": tool, "args": args}], "message": "ok", "delay": None, **extra}


def test_every_registered_tool_has_a_sample():
    assert {tool.name for tool in REGISTRY.list()} == set(VALID_ARGS)


@pytest.mark.parametrize("tool", REGISTRY.list(), ids=lambda t: t.name)
def test_schema_fields_match_function_parameters(tool):
    assert set(tool.args_schema.model_fields) == set(inspect.signature(tool.function).parameters)


@pytest.mark.parametrize("name, args", VALID_ARGS.items())
def test_valid_args_are_accepted(name, args):
    plan = build_plan(_plan_data(name, args), REGISTRY)
    assert plan.steps[0].tool == name


def test_calendar_default_time_is_applied():
    plan = build_plan(_plan_data("calendar_add", {"title": "会議", "date": "2026/11/10"}), REGISTRY)
    assert plan.steps[0].args["time"] == "09:00"


@pytest.mark.parametrize("name, args", [
    ("open_app", {}),
    ("open_app", {"name": 123}),
    ("open_app", {"name": ""}),
    ("open_app", {"name": "/Applications/Evil.app"}),
    ("open_app", {"name": "-a"}),
    ("open_app", {"name": "Notes", "extra": "x"}),
    ("close_app", {"name": "../Evil"}),
    ("calendar_add", {"title": "t", "date": "2026-11-10"}),
    ("calendar_add", {"title": "t", "date": "2026/11/10", "time": 14}),
    ("calendar_add", {"title": "1行目\n2行目", "date": "2026/11/10"}),
    ("mail_send", {"to": "a@example.com,b@example.com", "subject": "s", "body": "b"}),
    ("mail_send", {"to": "a b@example.com", "subject": "s", "body": "b"}),
    ("mail_send", {"to": "not-an-address", "subject": "s", "body": "b"}),
    ("mail_send", {"to": "a@example.com", "subject": "", "body": "b"}),
    ("mail_send", {"to": "a@example.com", "subject": "s\nBcc: x", "body": "b"}),
    ("mail_send", {"to": "a@example.com", "subject": "s"}),
    ("take_screenshot", {"path": "/tmp/x"}),
    ("excel_open", {"path": ["a"]}),
])
def test_invalid_args_are_rejected(name, args):
    with pytest.raises(PlanValidationError):
        build_plan(_plan_data(name, args), REGISTRY)


@pytest.mark.parametrize("data", [
    None,
    [],
    "text",
    {"steps": "open_app"},
    {"steps": [{"args": {}}]},
    {"steps": [], "unknown": 1},
    {"steps": [{"tool": "open_app", "args": {"name": "Notes"}, "sudo": True}]},
    {"steps": [{"tool": "存在しない", "args": {}}]},
    {"steps": [], "delay": "30"},
    {"steps": [], "delay": -1},
    {"steps": [], "delay": MAX_DELAY_SECONDS + 1},
    {"steps": [{"tool": "take_screenshot", "args": {}}] * (MAX_PLAN_STEPS + 1)},
])
def test_invalid_plans_are_rejected(data):
    with pytest.raises(PlanValidationError):
        build_plan(data, REGISTRY)


def test_zero_delay_runs_immediately():
    assert not build_plan({"steps": [], "delay": 0}, REGISTRY).delay


def test_plan_without_registry_cannot_contain_steps():
    with pytest.raises(PlanValidationError):
        build_plan(_plan_data("take_screenshot", {}), None)


def test_error_message_does_not_include_input_values():
    args = {"to": "a@example.com", "subject": "s", "body": SECRET, "unknown": SECRET}
    with pytest.raises(PlanValidationError) as e:
        build_plan(_plan_data("mail_send", args), REGISTRY)
    assert SECRET not in str(e.value)
    assert "unknown" in str(e.value)


# ── Planner ──

def _planner(replies):
    create = MagicMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])
        for r in replies
    ])
    planner = Planner(Context(), registry=REGISTRY)
    planner._classify = lambda *a: "easy"
    planner._ollama_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return planner, create


VALID_REPLY = json.dumps(_plan_data("take_screenshot", {}))


@pytest.mark.parametrize("reply", [
    VALID_REPLY,
    f"```json\n{VALID_REPLY}\n```",
    f"<think>考え中 ```</think>\n{VALID_REPLY}",
])
def test_planner_accepts_valid_reply_formats(reply):
    planner, _ = _planner([reply])
    assert planner.plan(Intent("スクショ")).steps[0].tool == "take_screenshot"


@pytest.mark.parametrize("reply", ["null", "```json\nnull\n```", "<think>x</think>null"])
def test_planner_treats_null_as_no_plan(reply):
    planner, _ = _planner([reply])
    assert planner.plan(Intent("依頼")) is None


def test_invalid_reply_retries_next_local_model():
    bad = json.dumps(_plan_data("存在しない", {}))
    planner, create = _planner([bad, VALID_REPLY])
    plan = planner.plan(Intent("スクショ"))
    assert plan.steps[0].tool == "take_screenshot"
    assert [c.kwargs["model"] for c in create.call_args_list] == [
        config.MODEL_ROUTER["easy"],
        config.MODEL_ROUTER["medium"],
    ]


@pytest.mark.parametrize("bad", ["これはJSONではありません", json.dumps(_plan_data("存在しない", {}))])
def test_all_invalid_replies_execute_nothing_and_explain(bad):
    planner, _ = _planner([bad, bad, bad])
    plan = planner.plan(Intent("依頼"))
    assert plan.steps == []
    assert "何も実行しませんでした" in plan.message


# ── Executor ──

def test_executor_validates_args_before_calling_tool():
    function = MagicMock()
    tool = SimpleNamespace(
        name="mail_send",
        function=function,
        args_schema=REGISTRY.get("mail_send").args_schema,
    )
    manager = ToolManager.__new__(ToolManager)
    manager.registry = SimpleNamespace(get=lambda name: tool)

    with pytest.raises(RuntimeError):
        manager.execute_plan(Plan(steps=[Step(tool="mail_send", args={"to": "bad"})]))

    function.assert_not_called()


def test_executor_passes_validated_args_including_defaults():
    function = MagicMock()
    tool = SimpleNamespace(
        name="calendar_add",
        function=function,
        args_schema=REGISTRY.get("calendar_add").args_schema,
    )
    manager = ToolManager.__new__(ToolManager)
    manager.registry = SimpleNamespace(get=lambda name: tool)

    manager.execute_plan(Plan(steps=[Step(tool="calendar_add", args={"title": "t", "date": "2026/11/10"})]))

    function.assert_called_once_with(title="t", date="2026/11/10", time="09:00")
