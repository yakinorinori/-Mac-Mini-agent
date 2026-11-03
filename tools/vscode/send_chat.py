import re

from agent.process import run_command

_QUOTE_PATTERNS = [
    r'"([^"]+)"',
    r"'([^']+)'",
    r"「([^」]+)」",
    r"“([^”]+)”",
    r"‘([^’]+)’",
]

_SAFETY_PREFIX = (
    "以下はTelegram経由の遠隔指示です。実装を進めてください。"
    "ただし、ファイルの変更・削除・コマンド実行など何らかの操作を行う前には、"
    "必ずチャットで確認を求め、私の許可なく勝手に実行しないでください。\n\n---\n"
)


def _extract_prompt(message: str) -> str:
    """メッセージ内の引用符(" ' 「」 " " ' ')で囲まれた範囲を抽出する。無ければ全文を使う。"""

    found = []
    for pattern in _QUOTE_PATTERNS:
        found.extend(re.findall(pattern, message))

    if found:
        return "\n".join(found)

    return message.strip()


def _escape_for_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_to_vscode_chat(message: str):
    """Telegramの指示をVS CodeのCopilot Chatへ自動入力して送信する。

    引用符で囲まれた範囲だけを実際のプロンプトとして抽出し、
    実行前確認を必須にする安全プレフィックスを付けて送信する。
    """

    prompt = _SAFETY_PREFIX + _extract_prompt(message)
    prompt = prompt.replace("\n", " ")  # keystrokeでのReturn誤送信を避けるため改行は空白に置換
    escaped = _escape_for_applescript(prompt)

    script = f'''
    tell application "Visual Studio Code 2" to activate
    delay 0.5

    tell application "System Events"
        keystroke "p" using {{shift down, command down}}
        delay 0.3
        keystroke "Chat: Focus on Chat View"
        delay 0.3
        key code 36
        delay 0.5
        keystroke "{escaped}"
        delay 0.2
        key code 36
    end tell
    '''

    run_command(["osascript", "-e", script])


def open_dev_mode():
    """開発モード開始時にVS Codeを起動し、Chatビューにフォーカスする(入力・送信はしない)。"""

    script = '''
    tell application "Visual Studio Code 2" to activate
    delay 0.5

    tell application "System Events"
        keystroke "p" using {shift down, command down}
        delay 0.3
        keystroke "Chat: Focus on Chat View"
        delay 0.3
        key code 36
        delay 0.5
    end tell
    '''

    run_command(["osascript", "-e", script])


def type_into_vscode_chat(text: str):
    """開発モード中、送られてきたテキストをChat入力欄にそのまま追記入力する(送信はしない)。"""

    escaped = _escape_for_applescript(text.replace("\n", " "))

    script = f'''
    tell application "Visual Studio Code 2" to activate
    delay 0.2

    tell application "System Events"
        keystroke "{escaped}"
    end tell
    '''

    run_command(["osascript", "-e", script])


def submit_vscode_chat():
    """Chat入力欄にたまっている内容をReturnで送信する。"""

    script = '''
    tell application "Visual Studio Code 2" to activate
    delay 0.2

    tell application "System Events"
        key code 36
    end tell
    '''

    run_command(["osascript", "-e", script])
