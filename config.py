import os

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """カンマ区切りのTelegramユーザーIDを解釈する。不正な値は黙って無視せず例外にする。"""

    ids = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdecimal() or int(part) <= 0:
            raise ValueError(
                "ALLOWED_TELEGRAM_USER_IDS には正の整数のIDをカンマ区切りで指定してください"
            )
        ids.add(int(part))

    return frozenset(ids)


# Botの操作を許可するTelegramユーザーID(例: "123456789" または "123,456")
ALLOWED_TELEGRAM_USER_IDS = parse_allowed_user_ids(
    os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
)

# Ollama設定
OLLAMA_BASE_URL = "http://localhost:11434/v1"

# 利用可能なモデル:
#   "qwen2.5:3b"     → 軽量・高速（1.9GB）  ← ツール選択など単純タスク向け
#   "qwen2.5:7b"     → バランス型（4.7GB）   ← 通常はこれ
#   "deepseek-r1:7b" → 推論特化（4.7GB）     ← 複雑な判断・多ステップタスク向け
OLLAMA_MODEL = "qwen2.5:7b"

# 難易度別モデルルーティング
# 難易度分類自体は常に最軽量モデルで行う
MODEL_ROUTER = {
    "easy":       "qwen2.5:3b",       # アプリ起動・ブックマークなど単純操作
    "medium":     "qwen2.5:7b",       # 検索・メール・カレンダーなど
    "hard":       "deepseek-r1:7b",   # 複雑な推論・多ステップタスク
    "api":        None,               # 外部送信は承認フロー実装まで行わない
    "impossible": None,               # どのモデルでも対応不可 → 「できません」
}


# ブックマーク一覧
# キー: 呼び名（自然言語でこの名前で呼ぶ）
# 値:   URL
BOOKMARKS = {
    "ChatGPT":    "https://chat.openai.com",
    "GitHub":     "https://github.com",
    "Gmail":      "https://mail.google.com",
    "YouTube":    "https://www.youtube.com",
    "Google":     "https://www.google.com",
    "Twitter":    "https://x.com",
    "Notion":     "https://www.notion.so",
    "Claude":     "https://claude.ai",
}

