"""返答の読み上げのテスト(say、ffmpeg、Telegram通信はモックする)。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler

import main
import speech
from agent.process import CommandError
from helpers import run_and_drain
from jobs import JobQueue
from store import Store

USER = 111
BUTTONS = InlineKeyboardMarkup([[InlineKeyboardButton("✅ 実行", callback_data="approve:x")]])


# ── speech.py ────────────────────────────────────

def test_speakable_removes_emoji_and_urls():
    text = "✅ 完了しました。\n🔍 結果: https://www.google.com/search?q=天気 を開きました。"
    assert speech.speakable(text) == "完了しました。 結果: リンク を開きました。"


def test_speakable_keeps_japanese_punctuation_and_long_vowels():
    assert speech.speakable("スクリーンショットを送りました。") == "スクリーンショットを送りました。"


def test_speakable_shortens_long_text():
    text = speech.speakable("あ" * (speech.MAX_SPOKEN_CHARS + 50))
    assert text.startswith("あ" * speech.MAX_SPOKEN_CHARS)
    assert text.endswith("以下は文字で確認してください。")


def test_speakable_of_only_emoji_is_empty():
    assert speech.speakable("✅🎉") == ""


def test_synthesize_passes_text_by_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(speech, "run_command", lambda args, timeout: calls.append(args))
    monkeypatch.setenv("SAY", "/usr/bin/say")
    monkeypatch.setenv("FFMPEG", "/bin/ffmpeg")
    monkeypatch.delenv("SAY_VOICE", raising=False)

    ogg = speech.synthesize("-v 送信", tmp_path)

    say, convert = calls
    assert say[:3] == ["/usr/bin/say", "-v", "Kyoko"]
    assert "-v 送信" not in say  # 文章は引数に入れない
    assert Path(say[say.index("-f") + 1]).read_text(encoding="utf-8") == "-v 送信"
    assert convert[0] == "/bin/ffmpeg"
    assert convert[convert.index("-c:a") + 1] == "libopus"
    assert convert[-1] == str(ogg)
    assert ogg.suffix == ".ogg"


def test_voice_can_be_changed(monkeypatch):
    monkeypatch.setenv("SAY_VOICE", "Otoya")
    assert speech.voice_name() == "Otoya"


def test_setup_problems(monkeypatch):
    monkeypatch.setattr(speech.shutil, "which", lambda name: None)
    monkeypatch.delenv("SAY", raising=False)
    monkeypatch.delenv("FFMPEG", raising=False)
    assert len(speech.setup_problems()) == 2


# ── 読み上げる文章の選択 ───────────────────────────

@pytest.mark.parametrize("text", [
    "⏳ 処理中です。",
    "📥 受け付けました（前に1件）。",
    "🎙 「画面ちょうだい」（文字起こし 3.0秒）",
    "🔧 操作フロー:\n1. take_screenshot()",
    "🧠 学習ルール「x」を外しました。",
])
def test_progress_messages_are_not_spoken(text):
    assert main.spoken_part(text) is None


def test_results_and_errors_are_spoken():
    assert main.spoken_part("スクリーンショットを送りました。") == "スクリーンショットを送りました。"
    assert main.spoken_part("⚠️ エラーが発生しました: x") == "⚠️ エラーが発生しました: x"


def test_approval_is_spoken_briefly():
    text = "🔧 操作フロー:\n1. mail_send(body=長い本文)\n\nこの内容で実行しますか？(10分で無効になります)"
    assert main.spoken_part(text, BUTTONS) == main.SPOKEN_APPROVAL


def test_other_buttons_are_not_spoken():
    assert main.spoken_part("🧠 「x」はqwenで3回成功しました。追加しますか？", BUTTONS) is None


def test_recording_message_forwards_everything():
    original = SimpleNamespace(chat_id=USER, reply_text=AsyncMock(), reply_photo=AsyncMock())
    recorder = main.RecordingMessage(original)

    asyncio.run(recorder.reply_text("⏳ 処理中です。"))
    asyncio.run(recorder.reply_text("完了しました。"))
    asyncio.run(recorder.reply_photo(b"png"))

    assert recorder.chat_id == USER
    assert original.reply_text.await_count == 2
    original.reply_photo.assert_awaited_once()
    assert recorder.spoken == ["完了しました。"]


# ── 音声メモの流れ ───────────────────────────────

def _voice_update():
    message = SimpleNamespace(
        text=None,
        chat_id=USER,
        voice=SimpleNamespace(duration=3, get_file=AsyncMock()),
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        reply_voice=AsyncMock(),
    )
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=USER))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main, "jobs", JobQueue())
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main.voice, "setup_problems", lambda: [])
    monkeypatch.setattr(main.speech, "setup_problems", lambda: [])
    monkeypatch.setattr(main, "transcribe_message", AsyncMock(return_value="画面ちょうだい"))
    spoken = []

    def fake_synthesize(text, workdir):
        spoken.append(text)
        path = Path(workdir) / "speech.ogg"
        path.write_bytes(b"ogg")
        return path

    monkeypatch.setattr(main.speech, "synthesize", fake_synthesize)

    async def fake_process_text(update, text, from_voice=False):
        await update.message.reply_text("⏳ 処理中です。")
        await update.message.reply_text("🔧 操作フロー:\n1. take_screenshot()")
        await update.message.reply_text("スクリーンショットを送りました。")

    monkeypatch.setattr(main, "process_text", fake_process_text)
    return SimpleNamespace(spoken=spoken)


def test_voice_memo_gets_spoken_reply(env):
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    assert env.spoken == ["スクリーンショットを送りました。"]
    update.message.reply_voice.assert_awaited_once()
    texts = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert "スクリーンショットを送りました。" in texts  # 文字の返信も残る


def test_no_spoken_reply_when_turned_off(env):
    main.store.set_setting(main.VOICE_REPLY_SETTING, "off")
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    assert env.spoken == []
    update.message.reply_voice.assert_not_awaited()


def test_speech_failure_is_reported_after_text_reply(env, monkeypatch):
    def broken(text, workdir):
        raise CommandError("say が失敗しました")

    monkeypatch.setattr(main.speech, "synthesize", broken)
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    texts = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert "スクリーンショットを送りました。" in texts
    assert texts[-1].startswith("🔇 読み上げに失敗しました")


def test_missing_speech_setup_is_reported(env, monkeypatch):
    monkeypatch.setattr(main.speech, "setup_problems", lambda: ["say（macOSのみ）"])
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    texts = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert texts[-1].startswith("🔇 読み上げが未導入です")
    update.message.reply_voice.assert_not_awaited()


def test_text_messages_are_not_spoken(monkeypatch):
    """文字で送った依頼には音声で返さない。"""
    synthesize = AsyncMock()
    monkeypatch.setattr(main.speech, "synthesize", synthesize)
    monkeypatch.setattr(main, "jobs", JobQueue())
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main.agent, "plan", lambda text: None)
    message = SimpleNamespace(text="こんにちは", chat_id=USER, reply_text=AsyncMock(), reply_voice=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=USER))

    run_and_drain(main.receive(update, None))

    message.reply_voice.assert_not_awaited()
    synthesize.assert_not_called()


# ── /voice ──────────────────────────────────────

def _command(args):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(message=message), SimpleNamespace(args=args)


def test_voice_command_toggles_and_sets():
    assert main.voice_reply_enabled()

    update, context = _command([])
    asyncio.run(main.toggle_voice_reply(update, context))
    assert not main.voice_reply_enabled()
    assert update.message.reply_text.call_args.args[0].startswith("🔇")

    update, context = _command([])
    asyncio.run(main.toggle_voice_reply(update, context))
    assert main.voice_reply_enabled()

    update, context = _command(["off"])
    asyncio.run(main.toggle_voice_reply(update, context))
    update, context = _command(["off"])
    asyncio.run(main.toggle_voice_reply(update, context))
    assert not main.voice_reply_enabled()


def test_voice_command_is_registered(monkeypatch):
    monkeypatch.setattr(main, "TOKEN", "123:abc")
    app = main.build_application()
    commands = [h for h in app.handlers[0] if isinstance(h, CommandHandler) and h.callback is main.toggle_voice_reply]
    assert len(commands) == 1
    assert "voice" in commands[0].commands


def test_setting_survives_reopen(tmp_path):
    path = tmp_path / "agent.db"
    Store(path).set_setting("voice_reply", "off")
    assert Store(path).get_setting("voice_reply", "on") == "off"
    assert Store(path).get_setting("missing", "x") == "x"
