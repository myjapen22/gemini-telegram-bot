import base64
import os
import unittest
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


class GeminiTTSRequestTests(unittest.TestCase):
    def test_payload_preserves_burmese_transcript_and_uses_interactions_schema(self):
        for transcript in ("နေကောင်းလား", "ဘာလုပ်နေလည်းဗျာ"):
            with self.subTest(transcript=transcript):
                payload = bot._build_gemini_tts_payload(
                    transcript, "Kore", "normal", "neutral", "gemini-3.8-flash-tts"
                )
                content = payload["input"][0]["content"][0]
                self.assertEqual(content["text"], transcript)
                self.assertEqual(content["type"], "text")
                self.assertEqual(
                    content["annotations"][0]["type"], "speech_metadata"
                )
                self.assertIn("style", content["annotations"][0])
                self.assertEqual(
                    payload["generation_config"]["speech_config"],
                    [{"voice": "Kore"}],
                )
                self.assertEqual(
                    payload["response_format"],
                    {
                        "type": "audio",
                        "mime_type": "audio/l16",
                        "sample_rate": 24000,
                    },
                )
                self.assertNotIn("voice_config", payload)
                self.assertNotIn("delivery_instruction", payload)

    def test_request_uses_api_key_header_and_decodes_interactions_audio(self):
        audio_b64 = base64.b64encode(b"mock signed 16-bit PCM").decode("ascii")
        response = FakeResponse(
            {
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "audio", "data": audio_b64}],
                    }
                ]
            }
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
        audio_b64 = base64.b64encode(b"pcm").decode("ascii")
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

    def test_generate_audio_segment_decodes_pcm(self):
        audio_b64 = base64.b64encode(b"raw-24khz-pcm").decode("ascii")
        with patch("bot._generate_audio_base64", return_value=audio_b64):
            self.assertEqual(
                bot.generate_audio_segment(
                    "နေကောင်းလား", "Kore", "normal", "neutral"
                ),
                b"raw-24khz-pcm",
            )


if __name__ == "__main__":
    unittest.main()
