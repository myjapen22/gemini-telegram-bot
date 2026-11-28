import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TELEGRAM_BOT_TOKEN"] = "123456:dummy"
os.environ["GEMINI_API_KEY"] = "dummy-gemini-key"
os.environ["VOICE_CLONE_API_KEY"] = "dummy-fish-key"
os.environ["ALLOWED_TELEGRAM_USER_IDS"] = "1"
os.environ["PORT"] = "0"

import bot  # noqa: E402


class FakeStatus:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class FakeChat:
    async def send_action(self, action):
        return None


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.status = None
        self.document_payload = None
        self.voice_payload = None
        self.chat = FakeChat()

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        self.status = FakeStatus()
        return self.status

    async def reply_document(self, document, filename=None, caption=None):
        self.document_payload = document.read()
        self.document_filename = filename
        self.document_caption = caption

    async def reply_voice(self, voice, caption=None):
        self.voice_payload = voice.read()
        self.voice_caption = caption


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.edits = []

    async def answer(self):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def owner_update(message=None, callback_query=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
        callback_query=callback_query,
    )


def response(status, payload=None, content=b"audio-bytes"):
    return SimpleNamespace(
        ok=200 <= status < 300,
        status_code=status,
        content=content,
        headers={"Content-Type": "audio/mpeg"},
        json=lambda: payload or {},
    )


class ElevenLabsKeyConfigurationTests(unittest.TestCase):
    def test_comma_separated_list_falls_back_to_legacy_single_key_and_deduplicates(self):
        with patch.dict(
            os.environ,
            {"ELEVENLABS_API_KEYS": " alpha, beta ,,alpha ", "ELEVENLABS_API_KEY": "fallback"},
            clear=True,
        ):
            self.assertEqual(bot._configured_elevenlabs_api_keys(), ["alpha", "beta"])
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": " one-key "}, clear=True):
            self.assertEqual(bot._configured_elevenlabs_api_keys(), ["one-key"])

    def test_key_pool_accepts_more_than_one_hundred_keys(self):
        keys = ",".join(f"key-{index}" for index in range(125))
        with patch.dict(os.environ, {"ELEVENLABS_API_KEYS": keys}, clear=True):
            configured = bot._configured_elevenlabs_api_keys()
        self.assertEqual(len(configured), 125)
        self.assertEqual(configured[-1], "key-124")


class ElevenLabsRotationTests(unittest.TestCase):
    def setUp(self):
        self.keys_patch = patch.object(bot, "ELEVENLABS_API_KEYS", ["key-one", "key-two", "key-three"])
        self.keys_patch.start()
        self.index_patch = patch.object(bot, "ELEVENLABS_ACTIVE_KEY_INDEX", 0)
        self.index_patch.start()
        self.addCleanup(self.index_patch.stop)
        self.addCleanup(self.keys_patch.stop)

    def test_invalid_first_key_rotates_and_keeps_the_successful_key_active(self):
        with patch.object(
            bot.requests,
            "post",
            side_effect=[
                response(401, {"detail": {"status": "invalid_api_key", "message": "Invalid key"}}),
                response(200),
            ],
        ) as post:
            result, index = bot._elevenlabs_request("https://example.test/tts", "tts", json={"text": "ဟယ်လို"})
        self.assertTrue(result.ok)
        self.assertEqual(index, 1)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["xi-api-key"], "key-one")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["xi-api-key"], "key-two")
        self.assertEqual(bot.ELEVENLABS_ACTIVE_KEY_INDEX, 1)

    def test_quota_error_rotates_but_concurrency_429_does_not(self):
        with patch.object(
            bot.requests,
            "post",
            side_effect=[
                response(400, {"detail": {"status": "quota_exceeded", "message": "No quota"}}),
                response(200),
            ],
        ) as post:
            _, index = bot._elevenlabs_request("https://example.test/tts", "tts")
        self.assertEqual(index, 1)
        self.assertEqual(post.call_count, 2)

        with patch.object(
            bot.requests,
            "post",
            return_value=response(429, {"detail": {"code": "concurrent_limit_exceeded", "message": "Wait"}}),
        ) as post:
            with self.assertRaises(bot.ElevenLabsAPIError) as raised:
                bot._elevenlabs_request("https://example.test/tts", "tts")
        self.assertEqual(raised.exception.status_code, 429)
        post.assert_called_once()

    def test_provider_error_never_exposes_a_key_in_exception_text(self):
        with patch.object(
            bot.requests,
            "post",
            return_value=response(401, {"detail": {"status": "invalid_api_key", "message": "Invalid key"}}),
        ):
            with self.assertRaises(bot.ElevenLabsAPIError) as raised:
                bot._elevenlabs_request("https://example.test/tts", "tts")
        self.assertNotIn("key-one", str(raised.exception))
        self.assertEqual(raised.exception.error_code, "invalid_api_key")


class ElevenLabsProviderTests(unittest.TestCase):
    def setUp(self):
        self.keys_patch = patch.object(bot, "ELEVENLABS_API_KEYS", ["key-one"])
        self.keys_patch.start()
        self.addCleanup(self.keys_patch.stop)

    def test_burmese_tts_uses_v3_language_code_auth_and_records_local_characters(self):
        with tempfile.TemporaryDirectory() as temp:
            usage_path = Path(temp) / "usage.json"
            with patch.object(bot, "ELEVENLABS_USAGE_FILE", usage_path), patch.object(
                bot.requests, "post", return_value=response(200, {}, b"mp3-bytes")
            ) as post:
                audio, index = bot._generate_elevenlabs_audio("မင်္ဂလာပါ။", "voice-123")
            self.assertEqual(audio, b"mp3-bytes")
            self.assertEqual(index, 0)
            args, kwargs = post.call_args
            self.assertEqual(args[0], "https://api.elevenlabs.io/v1/text-to-speech/voice-123")
            self.assertEqual(kwargs["headers"]["xi-api-key"], "key-one")
            self.assertEqual(kwargs["params"], {"output_format": "mp3_44100_128"})
            self.assertEqual(kwargs["json"]["text"], "မင်္ဂလာပါ။")
            self.assertEqual(kwargs["json"]["model_id"], "eleven_v3")
            self.assertEqual(kwargs["json"]["language_code"], "my")
            self.assertEqual(kwargs["timeout"], (15, 120))
            saved = __import__("json").loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(sum(saved["keys"].values()), len("မင်္ဂလာပါ။"))
            self.assertNotIn("key-one", usage_path.read_text(encoding="utf-8"))

    def test_voice_clone_uses_multipart_api_and_returns_voice_id(self):
        with tempfile.TemporaryDirectory() as temp:
            sample = Path(temp) / "sample.opus"
            sample.write_bytes(b"source audio")

            def transcode(_source, target):
                target.write_bytes(b"mp3 sample")

            payload = {"voice_id": "voice-abc", "requires_verification": False}
            with patch.object(bot, "transcode_elevenlabs_sample", side_effect=transcode), patch.object(
                bot.requests, "post", return_value=response(200, payload, b"")
            ) as post:
                voice_id, verification = bot.create_elevenlabs_voice(sample)
            self.assertEqual(voice_id, "voice-abc")
            self.assertFalse(verification)
            self.assertEqual(post.call_args.args[0], "https://api.elevenlabs.io/v1/voices/add")
            self.assertEqual(post.call_args.kwargs["headers"]["xi-api-key"], "key-one")
            self.assertIn("files", post.call_args.kwargs["files"])
            self.assertIn("Telegram Burmese clone", post.call_args.kwargs["data"]["name"])
            self.assertFalse((Path(temp) / "elevenlabs-reference.mp3").exists())

    def test_usage_report_is_local_and_labels_estimates_not_live_balances(self):
        with tempfile.TemporaryDirectory() as temp:
            usage_path = Path(temp) / "usage.json"
            with patch.object(bot, "ELEVENLABS_USAGE_FILE", usage_path), patch.object(
                bot, "ELEVENLABS_LOCAL_CHAR_QUOTA_PER_KEY", 10000
            ):
                bot.record_elevenlabs_usage("key-one", 250)
                report = bot.elevenlabs_usage_report(100, active_key_index=0)
            self.assertIn("100 characters", report)
            self.assertIn("9,750 / 10,000", report)
            self.assertIn("local estimate", report)
            self.assertIn("live balance မဟုတ်ပါ", report)
            self.assertIn("quota တစ်ခုတည်း", report)


class ElevenLabsMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_shows_all_requested_engine_and_help_options(self):
        message = FakeMessage("/start")
        await bot.start_command(owner_update(message), SimpleNamespace(user_data={}))
        text, kwargs = message.replies[0]
        self.assertIn("Burmese Text-to-Speech Bot", text)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        button_text = {button.text for button in buttons}
        self.assertIn("Gemini TTS", button_text)
        self.assertIn("ElevenLabs Voice Clone & TTS", button_text)
        self.assertIn("Fish Audio Voice Engine", button_text)
        self.assertIn("Check Remaining Credits / Usage Stats", button_text)
        self.assertIn("Help / Guide", button_text)

    async def test_switching_from_fish_to_gemini_clears_temporary_clone_state(self):
        with tempfile.TemporaryDirectory() as temp:
            clone_dir = Path(temp) / "clone"
            clone_dir.mkdir()
            sample = clone_dir / "reference.wav"
            sample.write_bytes(b"sample")
            user_data = {
                "active_tts_engine": "fish",
                "voice_clone_sample_path": str(sample),
                "voice_clone_transcript": "private transcript",
                "voice_clone_pending_transcript": True,
            }
            callback = FakeCallback("engine:gemini")
            await bot.callback_handler(owner_update(callback_query=callback), SimpleNamespace(user_data=user_data))
            self.assertEqual(user_data, {"active_tts_engine": "gemini"})
            self.assertFalse(clone_dir.exists())
            self.assertIn("Gemini TTS", callback.edits[0][0])

    async def test_selecting_elevenlabs_starts_clone_onboarding_with_plan_notice(self):
        callback = FakeCallback("engine:elevenlabs")
        with patch.object(bot, "ELEVENLABS_API_KEYS", ["key-one"]):
            user_data = {}
            await bot.callback_handler(owner_update(callback_query=callback), SimpleNamespace(user_data=user_data))
        self.assertEqual(user_data["active_tts_engine"], "elevenlabs")
        self.assertTrue(user_data["elevenlabs_pending_audio"])
        self.assertIn("Starter plan", callback.edits[0][0])
        self.assertIn("ခွင့်ပြုချက်", callback.edits[0][0])

    async def test_elevenlabs_text_sends_mp3_and_usage_breakdown(self):
        message = FakeMessage("မင်္ဂလာပါ")
        user_data = {"active_tts_engine": "elevenlabs", "elevenlabs_voice_id": "voice-abc"}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "speech.mp3"
            output.write_bytes(b"mp3 bytes")
            with patch.object(bot, "compile_elevenlabs_to_mp3", return_value=(output, 0)), patch.object(
                bot, "convert_to_voice_note", side_effect=RuntimeError("opus unavailable")
            ), patch.object(
                bot, "elevenlabs_usage_report", return_value="LOCAL USAGE REPORT"
            ), patch.object(bot, "ELEVENLABS_API_KEYS", ["key-one"]):
                await bot.handle_text(owner_update(message), SimpleNamespace(user_data=user_data))
        self.assertEqual(message.document_payload, b"mp3 bytes")
        self.assertEqual(message.document_filename, "elevenlabs-voiceover.mp3")
        self.assertIn("LOCAL USAGE REPORT", message.status.edits[-1])
        self.assertIn("100%", message.status.edits[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
