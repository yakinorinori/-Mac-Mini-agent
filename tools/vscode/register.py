from pydantic import Field

from agent.models import Tool
from agent.schemas import ToolArgs
from .send_chat import send_to_vscode_chat


class VscodeChatArgs(ToolArgs):
    message: str = Field(min_length=1, max_length=10000)


def register(registry):

    registry.register(
        Tool(
            name="send_to_vscode_chat",
            requires_approval=True,
            args_schema=VscodeChatArgs,
            function=send_to_vscode_chat,
            description="""
Mac mini上のVS Code(Copilot Chat)へ、遠隔で指示を送って実装を進めさせる。
「」や" "、' 'で囲まれた範囲だけが実際の指示として抽出されるので、
引数messageにはユーザーの発言をそのまま渡せばよい(自分で抜き出す必要はない)。
実行前には必ず確認を求めるよう、安全な前置き文が自動で付与される。

引数:
message  : ユーザーの発言そのもの(引用符で囲まれた部分が指示として使われる)

例
「認証機能を追加して」を実装して
VS Codeに"ボタンの色を青に変えて"と伝えて
"""
        )
    )
