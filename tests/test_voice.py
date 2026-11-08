"""音声メモの文字起こしと受け付けのテスト(外部コマンドとTelegram通信はモックする)。"""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import MessageHandler

import main
import voice
from agent.process import CommandError
from helpers import run_and_drain
from jobs import JobQueue

USER = 111


# ── voice.py ─────────────────────────────────────

def test_clean_transcript_joins_lines_and_drops_hallucinations():
    raw = " 明日の10時に会議を\n入れて。\n\nご視聴ありがとうございました。\n"
    assert voice.clean_transcript(raw) == "明日の10時に会議を入れて。"


def test_clean_transcript_of_only_hallucination_is_empty():
    assert voice.clean_transcript("ご視聴ありがとうございました\n") == ""


def test_setup_problems_lists_missing_parts(monkeypatch, tmp_path):
    monkeypatch.setattr(voice.shutil, "which", lambda name: None)
    monkeypatch.delenv("WHISPER_CLI", raising=False)
    monkeypatch.delenv("FFMPEG", raising=False)
    monkeypatch.setenv("WHISPER_MODEL", str(tmp_path / "none.bin"))

    problems = voice.setup_problems()

    assert len(problems) == 3
    assert "whisper-cpp" in problems[0]
    assert "ffmpeg" in problems[1]


def test_setup_ok_with_commands_and_model(monkeypatch, tmp_path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setenv("WHISPER_MODEL", str(model))

    assert voice.setup_problems() == []


def test_default_model_is_under_agent_models():
    assert voice.model_path() == Path(os.environ["HOME"]) / "agent" / "models" / voice.DEFAULT_MODEL_NAME


def test_transcribe_converts_then_runs_whisper(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, timeout):
        calls.append(args)
        if "-otxt" in args:
            base = Path(args[args.index("-of") + 1])
            base.with_suffix(".txt").write_text("画面ちょうだい。\n", encoding="utf-8")

    monkeypatch.setattr(voice, "run_command", fake_run)
    monkeypatch.setenv("WHISPER_CLI", "/bin/whisper-cli")
    monkeypatch.setenv("FFMPEG", "/bin/ffmpeg")
    monkeypatch.setenv("WHISPER_MODEL", "/models/m.bin")

    text = voice.transcribe(tmp_path / "in.oga", tmp_path)

    assert text == "画面ちょうだい。"
    convert, whisper = calls
    assert convert[0] == "/bin/ffmpeg"
    assert convert[convert.index("-i") + 1] == str(tmp_path / "in.oga")
    assert convert[convert.index("-ar") + 1] == "16000"
    assert whisper[0] == "/bin/whisper-cli"
    assert whisper[whisper.index("-l") + 1] == "ja"
    assert whisper[whisper.index("-m") + 1] == "/models/m.bin"
    assert whisper[whisper.index("-f") + 1] == convert[-1]


def test_cli_check_reports_missing(monkeypatch, capsys):
    monkeypatch.setattr(voice, "setup_problems", lambda: ["ffmpeg（brew install ffmpeg）"])

    assert voice._main(["--check"]) == 1
    assert "未導入" in capsys.readouterr().out


# ── main.py ─────────────────────────────────────

def _voice_update(duration=5, user_id=USER):
    message = SimpleNamespace(
        text=None,
        chat_id=user_id,
        voice=SimpleNamespace(duration=duration, get_file=AsyncMock()),
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=user_id))


def _texts(update):
    return [c.args[0] for c in update.message.reply_text.call_args_list]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main, "jobs", JobQueue())
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    monkeypatch.setattr(main.voice, "setup_problems", lambda: [])
    handled = []

    async def fake_process_text(update, text, from_voice=False):
        handled.append((text, from_voice))

    monkeypatch.setattr(main, "process_text", fake_process_text)
    return SimpleNamespace(handled=handled)


def _telegram_update(user_id, voice_message=True):
    from datetime import datetime, timezone
    from telegram import Chat, Message, Update, User, Voice

    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=Chat.PRIVATE),
        from_user=User(id=user_id, first_name="u", is_bot=False),
        voice=Voice("f", "u", duration=3) if voice_message else None,
        text=None if voice_message else "こんにちは",
    )
    return Update(update_id=1, message=message)


def test_voice_handler_accepts_only_allowed_users_voice(monkeypatch):
    monkeypatch.setattr(main, "TOKEN", "123:abc")
    monkeypatch.setattr(main, "ALLOWED_TELEGRAM_USER_IDS", frozenset({USER}))
    app = main.build_application()
    handlers = [h for h in app.handlers[0] if isinstance(h, MessageHandler) and h.callback is main.receive_voice]

    assert len(handlers) == 1
    handler = handlers[0]
    assert handler.check_update(_telegram_update(USER))
    assert not handler.check_update(_telegram_update(222))
    assert not handler.check_update(_telegram_update(USER, voice_message=False))


def test_voice_is_transcribed_and_processed_as_text(env, monkeypatch):
    monkeypatch.setattr(main, "transcribe_message", AsyncMock(return_value="画面ちょうだい"))
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    assert env.handled == [("画面ちょうだい", True)]
    assert _texts(update)[0].startswith("🎙 「画面ちょうだい」")


def test_long_voice_is_rejected_without_queueing(env, monkeypatch):
    transcribe = AsyncMock()
    monkeypatch.setattr(main, "transcribe_message", transcribe)
    update = _voice_update(duration=main.MAX_VOICE_SECONDS + 1)

    run_and_drain(main.receive_voice(update, None))

    transcribe.assert_not_called()
    assert env.handled == []
    assert "秒まで" in _texts(update)[0]


def test_empty_transcript_is_not_processed(env, monkeypatch):
    monkeypatch.setattr(main, "transcribe_message", AsyncMock(return_value=""))
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    assert env.handled == []
    assert "聞き取れませんでした" in _texts(update)[0]


def test_missing_setup_is_reported(env, monkeypatch):
    monkeypatch.setattr(main.voice, "setup_problems", lambda: ["ffmpeg（brew install ffmpeg）"])
    transcribe = AsyncMock()
    monkeypatch.setattr(main, "transcribe_message", transcribe)
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    transcribe.assert_not_called()
    assert "未導入" in _texts(update)[0]


def test_transcription_failure_is_reported(env, monkeypatch):
    monkeypatch.setattr(
        main, "transcribe_message", AsyncMock(side_effect=CommandError("whisper-cli が300秒以内に終了しませんでした"))
    )
    update = _voice_update()

    run_and_drain(main.receive_voice(update, None))

    assert env.handled == []
    assert "文字起こしに失敗" in _texts(update)[0]


def test_transcribe_message_downloads_and_removes_audio(monkeypatch):
    seen = {}

    async def download(path):
        Path(path).write_bytes(b"opus")
        seen["audio"] = Path(path)

    file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
    message = SimpleNamespace(voice=SimpleNamespace(get_file=AsyncMock(return_value=file)))

    def fake_transcribe(audio, workdir):
        assert audio.read_bytes() == b"opus"
        return "テスト"

    monkeypatch.setattr(main.voice, "transcribe", fake_transcribe)

    import asyncio
    assert asyncio.run(main.transcribe_message(message)) == "テスト"
    assert not seen["audio"].parent.exists()


# ── 開発モード ───────────────────────────────────

@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setattr(main.app_context, "dev_mode", True)
    typed, submitted = [], []
    monkeypatch.setattr(main, "type_into_vscode_chat", typed.append)
    monkeypatch.setattr(main, "submit_vscode_chat", lambda: submitted.append(True))
    return SimpleNamespace(typed=typed, submitted=submitted)


def _text_update(text):
    return SimpleNamespace(
        message=SimpleNamespace(text=text, chat_id=USER, reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=USER),
    )


def test_voice_send_word_is_typed_not_submitted_in_dev_mode(dev):
    import asyncio
    update = _text_update("送信")

    asyncio.run(main.handle_text(update, "送信", from_voice=True))

    assert dev.typed == ["送信"]
    assert dev.submitted == []
    assert main.app_context.dev_mode is True


def test_typed_send_word_still_submits_in_dev_mode(dev):
    import asyncio
    update = _text_update("送信")

    asyncio.run(main.handle_text(update, "送信"))

    assert dev.submitted == [True]
    assert dev.typed == []


def test_voice_cannot_start_dev_mode(monkeypatch):
    import asyncio
    monkeypatch.setattr(main.app_context, "dev_mode", False)
    opened = MagicMock()
    monkeypatch.setattr(main, "open_dev_mode", opened)
    monkeypatch.setattr(main.agent, "plan", lambda text: None)
    update = _text_update("開発モード")

    asyncio.run(main.handle_text(update, "開発モード", from_voice=True))

    opened.assert_not_called()
    assert main.app_context.dev_mode is False
