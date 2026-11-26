import asyncio
import base64
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Ensure this suite never consumes real Telegram or Gemini credentials.
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:dummy"
os.environ["GEMINI_API_KEY"] = "dummy-gemini-key"
os.environ["VOICE_CLONE_API_KEY"] = "dummy-fish-key"
os.environ["ALLOWED_TELEGRAM_USER_IDS"] = "1"
os.environ["PORT"] = "0"

import bot  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.payload


def short_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


class GeminiTTSRequestTests(unittest.TestCase):
    def test_payload_preserves_burmese_and_uses_documented_interactions_wav_schema(self):
        for transcript in ("နေကောင်းလား", "ဘာလုပ်နေလည်းဗျာ"):
            with self.subTest(transcript=transcript):
                payload = bot._build_gemini_tts_payload(
                    transcript, "Kore", "normal", "neutral", "gemini-3.8-flash-tts"
                )
                content = payload["input"][0]["content"][0]
                self.assertEqual(content["text"], transcript)
                self.assertEqual(content["type"], "text")
                self.assertEqual(content["annotations"][0]["type"], "speech_metadata")
                self.assertIn("style", content["annotations"][0])
                self.assertEqual(payload["generation_config"]["speech_config"], [{"voice": "Kore"}])
                # Unary Interactions audio output defaults to WAV; do not add PCM-only parameters.
                self.assertEqual(payload["response_format"], {"type": "audio"})
                self.assertNotIn("voice_config", payload)
                self.assertNotIn("mime_type", payload["response_format"])
                self.assertNotIn("delivery_instruction", payload)

    def test_request_uses_api_key_header_and_decodes_interactions_audio(self):
        audio_b64 = base64.b64encode(short_wav()).decode("ascii")
        response = FakeResponse(
            {"steps": [{"type": "model_output", "content": [{"type": "audio", "data": audio_b64}]}]}
        )
        with patch("bot.requests.post", return_value=response) as post:
            result = bot._generate_audio_base64(
                "နေကောင်းလား", "Kore", "gemini-3.8-flash-tts", "normal", "neutral"
            )
        self.assertEqual(result, audio_b64)
        args, kwargs = post.call_args
        self.assertEqual(args[0], bot.GEMINI_INTERACTIONS_URL)
        self.assertNotIn("key=", args[0])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "dummy-gemini-key")
        self.assertEqual(kwargs["json"]["input"][0]["content"][0]["text"], "နေကောင်းလား")
        self.assertEqual(kwargs["timeout"], 60)

    def test_extracts_sdk_style_output_audio_too(self):
        audio_b64 = base64.b64encode(short_wav()).decode("ascii")
        self.assertEqual(
            bot._extract_interaction_audio_base64({"output_audio": {"data": audio_b64}}),
            audio_b64,
        )

    def test_missing_audio_is_reported_as_invalid_response(self):
        with patch("bot.requests.post", return_value=FakeResponse({"steps": []})):
            with self.assertRaisesRegex(RuntimeError, "No audio data returned"):
                bot._generate_audio_base64(
                    "ဘာလုပ်နေလည်းဗျာ", "Kore", "gemini-3.8-flash-tts", "normal", "neutral"
                )

    def test_http_error_is_preserved_for_model_fallback_and_user_message(self):
        response = FakeResponse(
            {"error": {"message": "invalid request"}},
            status_code=400,
            text='{"error":{"message":"invalid request"}}',
        )
        with patch("bot.requests.post", return_value=response):
            with self.assertRaises(bot.GeminiTTSAPIError) as raised:
                bot._generate_audio_base64(
                    "နေကောင်းလား", "Kore", "gemini-3.8-flash-tts", "normal", "neutral"
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Gemini rejected the text-to-speech request", bot.tts_failure_message(raised.exception))

    def test_gemini_timeout_has_gemini_specific_user_message(self):
        import requests
        with patch("bot.requests.post", side_effect=requests.exceptions.Timeout):
            with self.assertRaises(bot.GeminiTTSRequestError) as raised:
                bot._generate_audio_base64(
                    "နေကောင်းလား", "Kore", "gemini-3.8-flash-tts", "normal", "neutral"
                )
        self.assertIn("Gemini TTS request timed out", bot.tts_failure_message(raised.exception))

    def test_generate_audio_segment_decodes_wav_response_bytes(self):
        wav_bytes = short_wav()
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
        with patch("bot._generate_audio_base64", return_value=audio_b64):
            self.assertEqual(bot.generate_audio_segment("နေကောင်းလား", "Kore", "normal", "neutral"), wav_bytes)


class VoiceCatalogAndScriptTests(unittest.TestCase):
    EXPECTED_VOICES = [
        "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe",
        "Callirrhoe", "Charon", "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux",
        "Iapetus", "Kore", "Laomedeia", "Leda", "Orus", "Pulcherrima", "Puck",
        "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
        "Vindemiatrix", "Zephyr", "Zubenelgenubi",
    ]

    def test_all_30_valid_prebuilt_voices_are_selectable_in_compact_menu(self):
        self.assertEqual([name for name, _, _ in bot.VOICE_CATALOG], self.EXPECTED_VOICES)
        self.assertEqual(set(bot.VOICES), set(self.EXPECTED_VOICES))
        keyboard = bot.voice_keyboard().inline_keyboard
        buttons = [button for row in keyboard[:-1] for button in row]
        self.assertEqual(len(buttons), 30)
        self.assertEqual({button.callback_data for button in buttons}, {f"voice:{name}" for name in self.EXPECTED_VOICES})
        self.assertEqual(keyboard[-1][0].callback_data, "menu:main")

    def test_burmese_and_english_labels_group_blocks_and_assign_roles(self):
        script = (
            "ဇာတ်ကြောင်းပြောသူ: ဒီနေ့ ဇာတ်လမ်းကို စမယ်။\nဆက်လက် ပြောပါမယ်။\n\n"
            "Female Hero: ဘာလုပ်နေလည်းဗျာ\nနေကောင်းပါတယ်။\n\n"
            "Male: နေကောင်းလား"
        )
        parsed = bot.parse_script(script)
        self.assertEqual(
            parsed,
            [
                ("ဇာတ်ကြောင်းပြောသူ", "ဒီနေ့ ဇာတ်လမ်းကို စမယ်။ ဆက်လက် ပြောပါမယ်။"),
                ("Female Hero", "ဘာလုပ်နေလည်းဗျာ နေကောင်းပါတယ်။"),
                ("Male", "နေကောင်းလား"),
            ],
        )
        expanded = bot.expand_script_segments(script)
        self.assertEqual([segment[2] for segment in expanded], [bot.current_voice, "Kore", "Puck"])
        self.assertTrue(all("Female Hero:" not in segment[1] for segment in expanded))

    def test_unicode_safe_chunking_handles_long_burmese_text(self):
        text = ("မြန်မာစာသားအတွက် စမ်းသပ်စာကြောင်း။ " * 40).strip()
        chunks = bot.split_text_for_tts(text, max_chunk_len=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        # Also test a no-space run so the implementation must find Unicode-safe boundaries.
        joined_clusters = "က\u103c\u102f" * 30
        cluster_chunks = bot.split_text_for_tts(joined_clusters, max_chunk_len=18)
        self.assertGreater(len(cluster_chunks), 1)
        self.assertTrue(all(len(chunk) <= 18 for chunk in cluster_chunks))
        self.assertTrue(all(not chunk or not __import__("unicodedata").category(chunk[0]).startswith("M") for chunk in cluster_chunks))

    def test_multi_speaker_compiler_writes_combined_mp3_and_reports_segment_progress(self):
        wav_bytes = short_wav()
        segments = [("Narrator", "First line", "Kore"), ("Hero", "Second line", "Puck")]
        completed = []
        result = None
        try:
            with patch.object(bot, "generate_audio_segment", side_effect=[wav_bytes, wav_bytes]), patch.object(
                bot, "TTS_INTER_SEGMENT_DELAY_SECONDS", 0
            ):
                result = bot.compile_script_to_mp3(
                    "Narrator: First line\nHero: Second line",
                    "multi",
                    "normal",
                    "neutral",
                    lambda done, total: completed.append((done, total)),
                    segments,
                )
            self.assertEqual(result.name, "voiceover.mp3")
            self.assertGreater(result.stat().st_size, 0)
            self.assertEqual(completed, [(1, 2), (2, 2)])
        finally:
            if result:
                shutil.rmtree(result.parent, ignore_errors=True)


class FakeStatusMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.status = None
        self.chat = SimpleNamespace(send_action=lambda action: asyncio.sleep(0))
        self.document_payload = None
        self.document_filename = None

    async def reply_text(self, text):
        self.status = FakeStatusMessage()
        return self.status

    async def reply_voice(self, voice, caption=None):
        return None

    async def reply_document(self, document, filename=None, caption=None):
        self.document_payload = document.read()
        self.document_filename = filename


class NormalTTSHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_speaker_handler_sends_voiceover_and_segment_progress(self):
        text = "Narrator: First line.\nAlice: Second line."
        message = FakeMessage(text)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        context = SimpleNamespace(user_data={})
        callback_events = []

        with tempfile.TemporaryDirectory(prefix="normal-tts-handler-") as temp:
            output = Path(temp) / "voiceover.mp3"
            output.write_bytes(b"combined-mp3")

            def fake_compile(text, mode, speed, style, progress_callback, prepared_segments):
                self.assertEqual(mode, "multi")
                self.assertEqual(len(prepared_segments), 2)
                progress_callback(1, 2)
                callback_events.append((1, 2))
                progress_callback(2, 2)
                callback_events.append((2, 2))
                return output

            with patch.object(bot, "current_voice_mode", "multi"), patch.object(
                bot, "compile_script_to_mp3", side_effect=fake_compile
            ), patch.object(bot, "convert_to_voice_note", side_effect=RuntimeError("not needed")):
                await bot.handle_text(update, context)

            self.assertEqual(message.document_filename, "voiceover.mp3")
            self.assertEqual(message.document_payload, b"combined-mp3")
            self.assertEqual(callback_events, [(1, 2), (2, 2)])
            status_history = "\n".join(message.status.edits)
            self.assertIn("95%", status_history)
            self.assertIn("100%", status_history)

    async def test_progress_animation_advances_to_complete_segment_percentage(self):
        status = FakeStatusMessage()

        async def no_wait(_seconds):
            return None

        fake_asyncio = SimpleNamespace(sleep=no_wait, CancelledError=asyncio.CancelledError)
        with patch.object(bot, "asyncio", fake_asyncio):
            await bot.animate_progress(
                status,
                "Generating TTS audio",
                {"completed": 2, "total": 2},
                threading.Lock(),
            )
        self.assertTrue(any("85%" in entry for entry in status.edits))
        self.assertTrue(any("90%" in entry for entry in status.edits))


if __name__ == "__main__":
    unittest.main(verbosity=2)
