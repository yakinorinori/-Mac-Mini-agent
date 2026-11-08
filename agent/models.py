from dataclasses import dataclass, field
from typing import Dict, Any, Callable, Optional, List, Type
from datetime import datetime
from enum import Enum

from pydantic import BaseModel

class MemoryType(str, Enum):
    EPISODIC = "episodic"   # 実行履歴の要約
    SEMANTIC = "semantic"   # 恒久的な事実・ルール・好み
    WORKING = "working"     # 進行中タスクの一時記憶

@dataclass
class Intent:
    text: str


@dataclass
class Command:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    delay: Optional[int] = None


@dataclass
class Step:
    """操作フローの1ステップ(実行するツールと、選んだ理由)。"""
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class Plan:
    """LLMが生成した操作フロー全体(実行前にユーザーへ提示する)。"""
    steps: List[Step] = field(default_factory=list)
    message: str = ""
    delay: Optional[int] = None
    # ユーザーの明示的な検索指示からコードが作ったPlanだけTrueにする。LLMの出力では設定できない
    preapproved: bool = False
    # 計測用。Plannerが設定し、LLMの出力では設定できない
    route: str = ""
    model: str = ""
    plan_seconds: float = 0.0

    def describe(self) -> str:
        """ユーザーに提示する「使用するツールと操作フロー」のテキストを組み立てる。"""
        if not self.steps:
            return self.message

        lines = ["🔧 操作フロー:"]
        for i, step in enumerate(self.steps, start=1):
            args_text = ", ".join(f"{k}={v}" for k, v in step.args.items())
            lines.append(f"{i}. {step.tool}({args_text}) — {step.reason}")
        return "\n".join(lines)


@dataclass
class Tool:
    name: str
    function: Callable
    description: str
    args_schema: Type[BaseModel]
    requires_approval: bool = False  # 実行前にTelegramでの承認を必須にする
