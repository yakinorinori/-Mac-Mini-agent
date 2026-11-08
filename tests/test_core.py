"""古い動作確認スクリプトを置き換える、基本部品の回帰テスト。

旧スクリプトはimport時にアプリを開いたり入力待ちをしたりしたため、自動テストに使えなかった。
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.agent import Agent
from agent.executor import ToolManager
from agent.models import Intent, Plan, Step, Tool
from agent.parser import Parser
from agent.planner import Planner
from agent.schemas import NoArgs
from context import Context
from registry.tool_registry import ToolRegistry
from scheduler import Scheduler


def test_parser_strips_whitespace():
    assert Parser().parse("  メモ \n") == Intent(text="メモ")


def test_agent_passes_parsed_intent_to_planner():
    agent = Agent(Context())
    agent.planner.plan = MagicMock(return_value="plan")
    assert agent.plan("  メモ ") == "plan"
    agent.planner.plan.assert_called_once_with(Intent(text="メモ"))


def test_context_keeps_last_plan_and_history():
    context = Context()
    first = Plan(steps=[Step(tool="open_app", args={"name": "Notes"})])
    second = Plan(steps=[Step(tool="take_screenshot")])
    context.add("メモ", first)
    context.add("スクショ", second)
    assert context.get_last_command() is second
    assert [entry["text"] for entry in context.get_history()] == ["メモ", "スクショ"]
    assert context.memory.latest()["command"] is second
    assert context.dev_mode is False


def test_repeat_returns_last_plan_without_llm():
    context = Context()
    planner = Planner(context)
    planner._classify = MagicMock(side_effect=AssertionError("LLMを呼びました"))
    assert planner.plan(Intent("もう一度")) is None
    plan = Plan(steps=[Step(tool="take_screenshot")])
    context.add("スクショ", plan)
    assert planner.plan(Intent("もう一度")) is plan


def test_registry_lookup():
    registry = ToolRegistry()
    tool = Tool(name="t", function=lambda: None, description="", args_schema=NoArgs)
    registry.register(tool)
    assert registry.get("t") is tool
    assert registry.get("missing") is None
    assert registry.list() == [tool]


def _manager(*tools):
    manager = ToolManager.__new__(ToolManager)
    manager.registry = ToolRegistry()
    for tool in tools:
        manager.registry.register(tool)
    return manager


def test_executor_runs_registered_tool():
    function = MagicMock(return_value="done")
    manager = _manager(Tool(name="t", function=function, description="", args_schema=NoArgs))
    assert manager.execute(Step(tool="t")) == "done"
    function.assert_called_once_with()


def test_executor_rejects_unknown_tool():
    with pytest.raises(ValueError):
        _manager().execute(Step(tool="missing"))


def test_executor_noop_does_nothing():
    assert _manager().execute(SimpleNamespace(tool="__noop__", args={})) is None


def test_all_plugins_load_from_real_tools_directory():
    names = {tool.name for tool in ToolManager().registry.list()}
    assert {"open_app", "close_app", "mail_send", "calendar_add", "take_screenshot"} <= names


def test_scheduler_runs_callback_after_delay():
    done = threading.Event()
    Scheduler().schedule(0.01, done.set)
    assert done.wait(2)


def test_scheduled_callback_can_be_cancelled():
    called = threading.Event()
    timer = Scheduler().schedule(0.2, called.set)
    timer.cancel()
    timer.join(1)
    assert not called.is_set()
