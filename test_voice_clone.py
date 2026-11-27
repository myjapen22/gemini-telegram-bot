import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Keep tests isolated from any real service credentials and make the owner check deterministic.
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:dummy"
os.environ["GEMINI_API_KEY"] = "dummy-gemini-key"
os.environ["VOICE_CLONE_API_KEY"] = "dummy"
os.environ["ALLOWED_TELEGRAM_USER_IDS"] = "1"
os.environ["PORT"] = "0"

import bot  # noqa: E402


class FakeStatusMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class FakeChat:
    def __init__(self):
        self.actions = []

    async def send_action(self, action):
        self.actions.append(action)


class FakeMessage:
    def __init__(self, text="", voice=None, audio=None, document=None):
        self.text = text
        self.voice = voice
        self.audio = audio
        self.document = document
        self.chat = FakeChat()
        self.reply_texts = []
        self.status = None
        self.voice_payload = None
        self.document_payload = None

    async def reply_text(self, text):
        self.reply_texts.append(text)
        self.status = FakeStatusMessage()
        return self.status

    async def reply_voice(self, voice, caption=None):
        self.voice_payload = voice.read()
        self.voice_caption = caption

    async def reply_document(self, document, filename=None, caption=None):
        self.document_payload = document.read()
        self.document_filename = filename
        self.document_caption = caption


class FakeTelegramFile:
    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(b"test audio payload")


class FakeTelegramBot:
    def __init__(self):
        self.get_file_calls = 0

    async def get_file(self, file_id):
        self.get_file_calls += 1
        return FakeTelegramFile()


def owner_update(message):
    return SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)


class DurationValidationTests(unittest.TestCase):
    def test_accepts_inclusive_duration_boundaries(self):
        bot.validate_voice_clone_sample_duration(bot.VOICE_CLONE_MIN_AUDIO_SECONDS)
        bot.validate_voice_clone_sample_duration(bot.VOICE_CLONE_MAX_AUDIO_SECONDS)

    def test_rejects_short_long_and_unverifiable_durations(self):
        for duration in (7, bot.VOICE_CLONE_MAX_AUDIO_SECONDS + 1, None, float("nan")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                bot.validate_voice_clone_sample_duration(duration)


class BurmeseClonePreprocessingTests(unittest.TestCase):
    def test_unicode_whitespace_and_myanmar_digits_are_normalized(self):
        text = "  မင်္ဂလာပါ\t၊\n\n\n ကျန်းမာပါသလား ၁၂၃  "
        self.assertEqual(
            bot.normalize_burmese_clone_text(text),
            "မင်္ဂလာပါ၊\n\nကျန်းမာပါသလား 123",
        )

    def test_burmese_combining_marks_and_joiners_are_preserved(self):
        text = "က\u1039\u1000\u103a\u200d\u1001\u200c"
        self.assertEqual(bot.normalize_burmese_clone_text(text), text)

    def test_optional_pronunciation_overrides_affect_synthesis_only(self):
        transcript = "ကျွန်တော် နေကောင်းပါတယ်။"
        with patch.object(
            bot, "VOICE_CLONE_PRONUNCIATION_OVERRIDES", {"ကျွန်တော်": "ကျနော်"}
        ):
            self.assertEqual(
                bot.normalize_burmese_clone_text(transcript, apply_pronunciation_overrides=True),
                "ကျနော် နေကောင်းပါတယ်။",
            )
            self.assertEqual(bot.clean_voice_clone_transcript(transcript), transcript)

    def test_markup_and_hidden_formatting_are_rejected_with_clear_feedback(self):
        for text in ("<b>hello</b>", "```python\nhello\n```", "hello\u202e"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                bot.normalize_burmese_clone_text(text)


class CloneHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_telegram_metadata_is_rejected_before_download(self):
        short_audio = SimpleNamespace(
            file_id="short-id", file_name="short.wav", mime_type="audio/wav",
            file_size=100, duration=7,
        )
        message = FakeMessage(audio=short_audio)
        telegram_bot = FakeTelegramBot()
        context = SimpleNamespace(user_data={"voice_clone_pending_audio": True}, bot=telegram_bot)

        await bot.clone_audio_handler(owner_update(message), context)

        self.assertIn("between 10 and 60 seconds", message.reply_texts[0])
        self.assertEqual(telegram_bot.get_file_calls, 0)

    async def test_unknown_duration_is_rejected_after_download(self):
        unknown_audio = SimpleNamespace(
            file_id="unknown-id", file_name="unknown.wav", mime_type="audio/wav",
            file_size=100, duration=None,
        )
        message = FakeMessage(audio=unknown_audio)
        telegram_bot = FakeTelegramBot()
        context = SimpleNamespace(user_data={"voice_clone_pending_audio": True}, bot=telegram_bot)

        with patch.object(bot, "probe_audio_duration", return_value=None), patch.object(
            bot, "transcode_clone_sample"
        ) as transcode:
            await bot.clone_audio_handler(owner_update(message), context)

        self.assertEqual(telegram_bot.get_file_calls, 1)
        self.assertFalse(transcode.called)
        self.assertTrue(any("Could not verify the audio duration" in text for text in message.status.edits))

    async def test_clone_text_sends_voice_note_and_mp3(self):
        with tempfile.TemporaryDirectory(prefix="clone-handler-test-") as temp:
            root = Path(temp)
            sample_path = root / "reference.wav"
            sample_path.write_bytes(b"sample")
            output_dir = root / "output"
            output_dir.mkdir()
            mp3_path = output_dir / "voice.mp3"
            mp3_path.write_bytes(b"mp3 payload")
            voice_note_path = output_dir / "voice.ogg"
            voice_note_path.write_bytes(b"opus payload")

            message = FakeMessage(text="Narrator: Hello there.\nAlice: How are you?")
            context = SimpleNamespace(
                user_data={
                    "voice_clone_sample_path": str(sample_path),
                    "voice_clone_transcript": "The exact reference words.",
                }
            )
            with patch.object(bot, "compile_cloned_voice_to_mp3", return_value=mp3_path) as compile_audio, patch.object(
                bot, "convert_to_voice_note", return_value=voice_note_path
            ):
                await bot.handle_text(owner_update(message), context)

            self.assertEqual(compile_audio.call_args.args[0], "Hello there. How are you?")
            self.assertEqual(message.voice_payload, b"opus payload")
            self.assertEqual(message.document_payload, b"mp3 payload")
            self.assertIn("Cloned voiceover ready", message.status.edits[-1])

    async def test_mp3_is_still_sent_when_voice_note_conversion_fails(self):
        with tempfile.TemporaryDirectory(prefix="clone-fallback-test-") as temp:
            root = Path(temp)
            sample_path = root / "reference.wav"
            sample_path.write_bytes(b"sample")
            output_dir = root / "output"
            output_dir.mkdir()
            mp3_path = output_dir / "voice.mp3"
            mp3_path.write_bytes(b"mp3 payload")

            message = FakeMessage(text="A short test line.")
            context = SimpleNamespace(
                user_data={
                    "voice_clone_sample_path": str(sample_path),
                    "voice_clone_transcript": "The exact reference words.",
                }
            )
            with patch.object(bot, "compile_cloned_voice_to_mp3", return_value=mp3_path), patch.object(
                bot, "convert_to_voice_note", side_effect=OSError("test conversion failure")
            ):
                await bot.handle_text(owner_update(message), context)

            self.assertIsNone(message.voice_payload)
            self.assertEqual(message.document_payload, b"mp3 payload")
            self.assertIn("voice-note format was unavailable", message.status.edits[-1])


    async def test_provider_timeout_is_reported_in_status_message(self):
        import requests

        with tempfile.TemporaryDirectory(prefix="clone-timeout-test-") as temp:
            sample_path = Path(temp) / "reference.wav"
            sample_path.write_bytes(b"sample")
            message = FakeMessage(text="A short test line.")
            context = SimpleNamespace(
                user_data={
                    "voice_clone_sample_path": str(sample_path),
                    "voice_clone_transcript": "The exact reference words.",
                }
            )
            with patch.object(
                bot, "compile_cloned_voice_to_mp3",
                side_effect=requests.exceptions.ReadTimeout("test timeout"),
            ):
                await bot.handle_text(owner_update(message), context)

            self.assertIn("timed out", message.status.edits[-1].lower())
            self.assertIsNone(message.document_payload)


    async def test_clone_input_respects_existing_max_text_limit(self):
        with tempfile.TemporaryDirectory(prefix="clone-length-test-") as temp:
            sample_path = Path(temp) / "reference.wav"
            sample_path.write_bytes(b"sample")
            message = FakeMessage(text="x" * (bot.MAX_TEXT_LENGTH + 1))
            context = SimpleNamespace(
                user_data={
                    "voice_clone_sample_path": str(sample_path),
                    "voice_clone_transcript": "The exact reference words.",
                }
            )
            with patch.object(bot, "compile_cloned_voice_to_mp3") as compile_audio:
                await bot.handle_text(owner_update(message), context)

            self.assertIn("Text is too long", message.reply_texts[0])
            compile_audio.assert_not_called()

    async def test_unsupported_formatting_is_reported_before_synthesis(self):
        message = FakeMessage(text="<b>Do not synthesize markup</b>")
        context = SimpleNamespace(
            user_data={
                "voice_clone_sample_path": "/tmp/reference.wav",
                "voice_clone_transcript": "exact transcript",
            }
        )
        with patch.object(bot, "compile_cloned_voice_to_mp3") as compile_audio:
            await bot.handle_text(owner_update(message), context)

        self.assertIn("no audio was generated", message.reply_texts[0])
        self.assertIn("plain text", message.reply_texts[0])
        compile_audio.assert_not_called()

    async def test_reference_transcript_is_cleaned_but_not_pronunciation_overridden(self):
        message = FakeMessage(text="  ကျွန်တော်   ၀၁၂\n နေကောင်းပါတယ်။  ")
        context = SimpleNamespace(user_data={"voice_clone_pending_transcript": True})
        with patch.object(
            bot, "VOICE_CLONE_PRONUNCIATION_OVERRIDES", {"ကျွန်တော်": "ကျနော်"}
        ):
            await bot.handle_text(owner_update(message), context)

        self.assertEqual(context.user_data["voice_clone_transcript"], "ကျွန်တော် ၀၁၂\nနေကောင်းပါတယ်။")
        self.assertIn("word for word", message.reply_texts[0])

    async def test_clone_setup_guides_user_to_clear_15_to_30_second_sample(self):
        message = FakeMessage(text="/clonevoice")
        context = SimpleNamespace(user_data={})
        await bot.clonevoice_command(owner_update(message), context)

        guidance = message.reply_texts[0]
        self.assertIn("15–30 seconds", guidance)
        self.assertIn("background noise", guidance)
        self.assertIn("word-for-word transcript", guidance)


class FishAudioErrorTests(unittest.TestCase):
    def test_request_timeout_is_bounded_and_reported_clearly(self):
        import requests

        with patch.object(bot.requests, "post", side_effect=requests.exceptions.ReadTimeout("test timeout")) as post:
            with self.assertRaises(requests.exceptions.ReadTimeout) as raised:
                bot._generate_voice_clone_audio("hello", b"sample", "exact transcript", "normal")

        self.assertEqual(post.call_args.kwargs["timeout"], (15, 120))
        self.assertIn("timed out", bot.tts_failure_message(raised.exception).lower())

    def test_http_429_is_reported_as_quota_or_rate_limit(self):
        response = SimpleNamespace(ok=False, status_code=429)
        with patch.object(bot.requests, "post", return_value=response):
            with self.assertRaises(bot.VoiceCloneAPIError) as raised:
                bot._generate_voice_clone_audio("hello", b"sample", "exact transcript", "normal")

        self.assertIn("rate limit", bot.tts_failure_message(raised.exception).lower())

    def test_request_uses_documented_fields_and_cleans_reference_transcript(self):
        response = SimpleNamespace(
            ok=True, status_code=200, content=b"mp3 bytes", headers={"Content-Type": "audio/mpeg"}
        )
        text = "  မင်္ဂလာပါ ၁၂၃  "
        transcript = "  နမူနာ   ၀၁၂\n စာသား  "
        with patch.object(bot.requests, "post", return_value=response) as post:
            audio = bot._generate_voice_clone_audio(text, b"wav reference", transcript, "normal")

        payload = bot.msgpack.unpackb(post.call_args.kwargs["data"], raw=False)
        self.assertEqual(audio, b"mp3 bytes")
        self.assertEqual(payload["text"], "မင်္ဂလာပါ 123")
        self.assertEqual(payload["references"][0]["audio"], b"wav reference")
        self.assertEqual(payload["references"][0]["text"], "နမူနာ ၀၁၂\nစာသား")
        self.assertEqual(payload["format"], "mp3")
        self.assertFalse(payload["normalize"])
        self.assertEqual(payload["sample_rate"], 44100)
        self.assertEqual(payload["mp3_bitrate"], 192)
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["top_p"], 0.7)
        self.assertEqual(payload["repetition_penalty"], 1.2)
        self.assertEqual(payload["prosody"]["speed"], 1.0)
        self.assertEqual(post.call_args.kwargs["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"], "application/msgpack")


if __name__ == "__main__":
    unittest.main(verbosity=2)
