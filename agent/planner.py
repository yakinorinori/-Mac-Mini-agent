import json
import re
from datetime import datetime
from openai import OpenAI
from agent.models import Command, Plan, Step
from config import OPENAI_API_KEY, OLLAMA_BASE_URL, MODEL_ROUTER


# 難易度の定義
DIFFICULTY_LEVELS = ["easy", "medium", "hard", "api", "impossible"]

# VS Code宛の引用符付き指示かどうかの判定用
_VSCODE_KEYWORDS = ("vscode", "vs code", "visual studio code")
_QUOTE_CHARS = re.compile(r'["\'「」“”‘’]')


def _is_vscode_chat_request(text: str) -> bool:
    """「VS Codeに"..."を送って」のような指示かを判定する(引用符の中身をLLMに解釈させないため)。"""
    lowered = text.lower()
    return any(k in lowered for k in _VSCODE_KEYWORDS) and bool(_QUOTE_CHARS.search(text))


class Planner:

    def __init__(self, context, registry=None):

        self.context = context
        self.registry = registry
        self._ollama_client = None
        self._openai_client = None

    # ── クライアント ──────────────────────────────

    @property
    def ollama_client(self):
        if self._ollama_client is None:
            self._ollama_client = OpenAI(
                base_url=OLLAMA_BASE_URL,
                api_key="ollama",
            )
        return self._ollama_client

    @property
    def openai_client(self):
        if self._openai_client is None:
            self._openai_client = OpenAI(api_key=OPENAI_API_KEY)
        return self._openai_client

    def _get_client(self, model: str):
        """モデル名からクライアントを選択"""
        if model.startswith("gpt-"):
            return self.openai_client
        return self.ollama_client

    # ── 難易度分類 ────────────────────────────────

    def _classify(self, text: str, tools_description: str) -> str:
        """
        qwen2.5:3b（最軽量）でタスクの難易度を分類する。
        返値: "easy" / "medium" / "hard" / "api" / "impossible"
        """

        prompt = f"""あなたはタスク難易度を分類するAIです。
ユーザーの指示と利用可能なツール一覧を見て、難易度を判定してください。

【利用可能なツール一覧】
{tools_description}

【ユーザーの指示】
{text}

【難易度の基準】
- easy      : アプリ起動・ブックマーク・URL開くなど1ステップで完結する単純操作
- medium    : 検索・メール送信・カレンダー追加など引数が複数必要な操作
- hard      : 複数ステップの推論が必要、または条件分岐・計算を含む複雑な操作
- api       : 上記ツール一覧では対応不可だが、高度なAIで対応できる可能性がある
- impossible: ツール一覧のどれを使っても実現できない、現実世界の物理的な行動
              （宇宙に行く、瞬間移動する、他人を殴る等）や実在しない/不可能な要求

【判定例】
- 「メモを開いて」→ easy
- 「田中さんにメールを送って」→ medium
- 「宇宙にロケットで行って」→ impossible
- 「空を飛んで移動して」→ impossible

【出力形式】
他の単語や説明を一切含めず、以下の5つのいずれか1単語のみを出力してください:
easy / medium / hard / api / impossible"""

        try:
            response = self.ollama_client.chat.completions.create(
                model=MODEL_ROUTER["easy"],  # 常に最軽量モデルで分類
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            level = response.choices[0].message.content.strip().lower()

            # 余計な文字が含まれる場合に対応
            for d in DIFFICULTY_LEVELS:
                if d in level:
                    print(f"[Planner] 難易度: {d}")
                    return d

            print(f"[Planner] 難易度分類失敗（応答: {level}）→ medium にフォールバック")
            return "medium"

        except Exception as e:
            print(f"[Planner] 難易度分類エラー: {e} → medium にフォールバック")
            return "medium"

    # ── コマンド生成 ──────────────────────────────

    def _generate_flow(self, text: str, model: str, tools_description: str) -> Plan:
        """指定モデルで自然言語 → 操作フロー(Plan)を生成する。複数ツールに跨る指示にも対応する。"""

        now = datetime.now()
        weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][now.weekday()]

        prompt = f"""あなたはMacMiniを操作するアシスタントです。
ユーザーの指示を実現するために必要な操作フローを、以下のツール一覧から選んで組み立ててください。
1つのツールで完結する場合はステップ1つだけでよく、複数ツールが必要な場合は実行順に並べてください。

【現在日時】
{now.strftime('%Y/%m/%d')}（{weekday_ja}曜日） {now.strftime('%H:%M')}
「今日」「明日」「来週」などの相対的な日付は、この現在日時を基準に計算してください。

【ツール一覧】
{tools_description}

【ユーザーの指示】
{text}

【出力形式】
以下のJSON形式のみで返してください。余計な説明は不要です。
{{
  "steps": [
    {{"tool": "ツール名", "args": {{"引数名": "値"}}, "reason": "このツールを選んだ理由"}}
  ],
  "message": "ユーザーへの返答（日本語・絵文字可）",
  "delay": null
}}

実行できるツールがない場合は null を返してください。
JSONのみ出力してください。"""

        client = self._get_client(model)

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )

        content = response.choices[0].message.content.strip()
        print(f"[Planner] モデル={model} 応答: {content}")

        if content.lower() == "null":
            return None

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        data = json.loads(content)

        steps = [
            Step(
                tool=s["tool"],
                args=s.get("args", {}),
                reason=s.get("reason", ""),
            )
            for s in data.get("steps", [])
        ]

        return Plan(
            steps=steps,
            message=data.get("message", ""),
            delay=data.get("delay"),
        )

    # ── メインロジック ────────────────────────────

    def plan(self, intent) -> Plan | None:

        # 「もう一度」は特別処理
        if intent.text.strip() == "もう一度":
            return self.context.get_last_command()

        # VS Code宛の引用符付き指示は、中身をLLMに解釈させず直接転送する
        if _is_vscode_chat_request(intent.text):
            print("[Planner] VS Code宛の引用指示を検出 → send_to_vscode_chatへ直接ルーティング")
            return Plan(
                steps=[
                    Step(
                        tool="send_to_vscode_chat",
                        args={"message": intent.text},
                        reason="引用符内の内容をそのままVS Code Chatへ転送するため",
                    )
                ],
                message="VS Codeに指示を送信しました。",
                delay=None,
            )

        tools_description = self._build_tools_description()

        # ① 難易度を分類
        difficulty = self._classify(intent.text, tools_description)

        # ② impossible → 即「できません」
        if difficulty == "impossible":
            print("[Planner] impossible → 対応不可")
            return Plan(
                steps=[],
                message="申し訳ありません、その操作はできません 🙏",
                delay=None,
            )

        # ③ 担当モデルを決定
        model = MODEL_ROUTER[difficulty]

        try:
            return self._generate_flow(intent.text, model, tools_description)

        except Exception as e:
            print(f"[Planner] モデル={model} エラー: {e}")

            # ④ 失敗したら1段階上のモデルにフォールバック
            idx = DIFFICULTY_LEVELS.index(difficulty)
            for next_level in DIFFICULTY_LEVELS[idx + 1:]:
                next_model = MODEL_ROUTER[next_level]
                if next_model is None:
                    break
                print(f"[Planner] フォールバック → {next_level}({next_model})")
                try:
                    return self._generate_flow(intent.text, next_model, tools_description)
                except Exception as e2:
                    print(f"[Planner] フォールバックも失敗: {e2}")

            return None

    # ── ツール一覧整形 ────────────────────────────

    def _build_tools_description(self):

        if self.registry is None:
            return "（ツール情報なし）"

        lines = []
        for tool in self.registry.list():
            lines.append(f"- {tool.name}:\n{tool.description.strip()}")

        return "\n\n".join(lines)
