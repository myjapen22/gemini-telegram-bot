import asyncio
import html
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Keep the suite deterministic and prevent calls to real APIs or Telegram.
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


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.statuses = []
        self.voice_payload = None
        self.audio_payload = None
        self.document_payload = None

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        status = FakeStatus()
        self.statuses.append(status)
        return status

    async def reply_voice(self, voice, caption=None):
        self.voice_payload = voice.read()
        self.voice_caption = caption

    async def reply_audio(self, audio, title=None, caption=None):
        self.audio_payload = audio.read()
        self.audio_title = title
        self.audio_caption = caption

    async def reply_document(self, document, filename=None, caption=None):
        self.document_payload = document.read()
        self.document_filename = filename
        self.document_caption = caption


class FakeCallback:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.edits = []
        self.answered = 0

    async def answer(self):
        self.answered += 1

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def owner_update(callback=None, message=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        callback_query=callback,
        message=message,
    )


class BurmeseVoiceGuideTests(unittest.TestCase):
    def test_all_30_voices_have_short_burmese_script_guides_and_gender(self):
        self.assertEqual(set(bot.VOICE_GUIDES), set(bot.VOICES))
        for voice, guide in bot.VOICE_GUIDES.items():
            with self.subTest(voice=voice):
                self.assertTrue(guide.strip())
                self.assertRegex(guide, r"[\u1000-\u109F]")
                self.assertNotRegex(guide, r"[A-Za-z]")
                self.assertLessEqual(len(guide), 120)
                self.assertEqual(bot.VOICES[voice]["description"], guide)
                self.assertIn(bot.VOICES[voice]["gender"], bot.voice_selection_text(voice))
                self.assertIn(guide, bot.voice_selection_text(voice))

    def test_voice_menu_is_three_columns_with_current_marker_and_back(self):
        with patch.object(bot, "current_voice", "Algieba"):
            keyboard = bot.voice_keyboard().inline_keyboard
        voice_rows = keyboard[:-1]
        self.assertTrue(all(len(row) == 3 for row in voice_rows))
        self.assertEqual(keyboard[-1][0].callback_data, "menu:main")
        buttons = [button for row in voice_rows for button in row]
        self.assertEqual(len(buttons), 30)
        self.assertIn("✓ Algieba", " ".join(button.text for button in buttons))
        self.assertEqual({button.callback_data for button in buttons}, {f"voice:{name}" for name in bot.VOICES})

    def test_voice_selector_and_style_selector_text_is_burmese_and_separate(self):
        self.assertRegex(bot.voice_menu_text(), r"[\u1000-\u109F]")
        self.assertRegex(bot.style_menu_text(), r"[\u1000-\u109F]")
        self.assertIn("30", bot.voice_menu_text())
        self.assertNotIn("preview", bot.voice_menu_text().lower())
        with patch.object(bot, "current_style", "dramatic_emotional"):
            self.assertIn("Dramatic &amp; Emotional", bot.welcome_text())


class VoicePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_voice_callback_shows_matching_guide_and_requests_preview(self):
        message = FakeMessage()
        query = FakeCallback("voice:Algieba", message)
        clone_data = {
            "voice_clone_sample_path": "/private/reference.wav",
            "voice_clone_transcript": "private transcript",
        }
        context = SimpleNamespace(user_data=clone_data)
        preview = AsyncMock()
        with patch.object(bot, "current_voice", "Kore"), patch.object(
            bot, "send_voice_preview", preview
        ):
            await bot.callback_handler(owner_update(callback=query), context)
            self.assertEqual(bot.current_voice, "Algieba")

        text = query.edits[0][0]
        self.assertIn("Algieba", text)
        self.assertIn("Male", text)
        self.assertIn(bot.VOICE_GUIDES["Algieba"], text)
        preview.assert_awaited_once_with(message, "Algieba")
        self.assertEqual(context.user_data, clone_data)

    async def test_opening_voice_or_style_menu_does_not_generate_preview(self):
        preview = AsyncMock()
        context = SimpleNamespace(user_data={})
        with patch.object(bot, "send_voice_preview", preview):
            for callback_data in ("menu:voice", "menu:style"):
                query = FakeCallback(callback_data)
                await bot.callback_handler(owner_update(callback=query), context)
                self.assertTrue(query.edits)
        preview.assert_not_awaited()

    async def test_preview_uses_normal_generator_with_selected_voice_and_sends_voice_note(self):
        message = FakeMessage()

        def write_mp3(_wav_path, mp3_path):
            mp3_path.write_bytes(b"short-mp3")

        def write_voice_note(mp3_path):
            ogg_path = mp3_path.with_suffix(".ogg")
            ogg_path.write_bytes(b"playable-voice-note")
            return ogg_path

        with patch.object(bot, "current_speed", "slow"), patch.object(
            bot, "current_style", "dark_horror"
        ), patch.object(bot, "generate_audio_segment", return_value=b"wav") as generate, patch.object(
            bot, "_convert_preview_wav_to_mp3", side_effect=write_mp3
        ), patch.object(bot, "convert_to_voice_note", side_effect=write_voice_note):
            await bot.send_voice_preview(message, "Algieba")

        self.assertEqual(generate.call_args.args, (bot.VOICE_PREVIEW_TEXT, "Algieba", "slow", "dark_horror"))
        self.assertEqual(message.voice_payload, b"playable-voice-note")
        self.assertIn("Algieba Burmese voice preview", message.voice_caption)
        self.assertIn("sent", message.statuses[0].edits[-1])

    async def test_preview_falls_back_to_mp3_attachment_when_voice_note_fails(self):
        message = FakeMessage()

        def write_mp3(_wav_path, mp3_path):
            mp3_path.write_bytes(b"preview-mp3")

        with patch.object(bot, "generate_audio_segment", return_value=b"wav"), patch.object(
            bot, "_convert_preview_wav_to_mp3", side_effect=write_mp3
        ), patch.object(bot, "convert_to_voice_note", side_effect=RuntimeError("no opus")):
            await bot.send_voice_preview(message, "Kore")

        self.assertEqual(message.audio_payload, b"preview-mp3")
        self.assertIn("Kore Burmese voice preview", message.audio_caption)

    async def test_preview_generation_failure_returns_burmese_error_and_keeps_bot_responsive(self):
        message = FakeMessage()
        with patch.object(bot, "generate_audio_segment", side_effect=RuntimeError("mock API error")):
            await bot.send_voice_preview(message, "Kore")
        self.assertRegex(message.statuses[0].edits[-1], r"[\u1000-\u109F]")
        self.assertIsNone(message.voice_payload)


class MovieStyleMenuTests(unittest.IsolatedAsyncioTestCase):
    EXPECTED_STYLES = {
        "Suspense & Thriller",
        "Dramatic & Emotional",
        "Fast-Paced Recap",
        "Action & Energetic",
        "Funny & Conversational",
        "Dark & Horror",
        "Local Burmese Street-Talk",
        "Epic & Cinematic Trailer",
        "Soft & Sad / Nostalgic",
        "Satirical & Sarcastic",
    }

    def test_ten_styles_have_burmese_hints_and_two_column_keyboard(self):
        self.assertEqual(set(bot.STYLE_NAMES.values()), self.EXPECTED_STYLES)
        keyboard = bot.style_keyboard().inline_keyboard
        self.assertEqual([len(row) for row in keyboard[:-1]], [2, 2, 2, 2, 2])
        self.assertEqual(keyboard[-1][0].callback_data, "menu:main")
        self.assertEqual(len([button for row in keyboard[:-1] for button in row]), 10)
        for hint in bot.STYLE_HINTS.values():
            self.assertRegex(hint, r"[\u1000-\u109F]")
        self.assertIn(bot.STYLE_NAMES[bot.current_style], bot.style_menu_text())

    def test_style_instructions_stay_in_metadata_and_do_not_modify_transcript(self):
        transcript = "ဒီနေ့ ဒီရုပ်ရှင်ဇာတ်လမ်းကို ပြန်ပြောပြမယ်။"
        for key, _, _, prompt in bot.STYLE_CATALOG:
            with self.subTest(style=key):
                payload = bot._build_gemini_tts_payload(
                    transcript, "Kore", "normal", key, "gemini-3.8-flash-tts"
                )
                content = payload["input"][0]["content"][0]
                self.assertEqual(content["text"], transcript)
                self.assertEqual(
                    content["annotations"][0]["style"],
                    f"{prompt}; {bot.SPEEDS['normal']}",
                )

    def test_voice_and_style_commands_are_registered_separately(self):
        app = bot.build_application()
        commands = {
            command
            for handler_group in app.handlers.values()
            for handler in handler_group
            if hasattr(handler, "commands")
            for command in handler.commands
        }
        self.assertIn("setvoice", commands)
        self.assertIn("setstyle", commands)

    async def test_style_menu_opens_with_current_selection_and_marked_button(self):
        query = FakeCallback("menu:style")
        with patch.object(bot, "current_style", "epic_cinematic_trailer"):
            await bot.callback_handler(owner_update(callback=query), SimpleNamespace(user_data={}))
            self.assertIn("Epic & Cinematic Trailer", html.unescape(query.edits[0][0]))
            buttons = [b for row in query.edits[0][1]["reply_markup"].inline_keyboard[:-1] for b in row]
            self.assertTrue(any("✓ Epic & Cinematic Trailer" == button.text for button in buttons))

    async def test_selecting_style_updates_current_value_and_confirms(self):
        query = FakeCallback("style:burmese_street_talk")
        with patch.object(bot, "current_style", "dramatic_emotional"):
            await bot.callback_handler(owner_update(callback=query), SimpleNamespace(user_data={}))
            self.assertEqual(bot.current_style, "burmese_street_talk")
        self.assertIn("Local Burmese Street-Talk", query.edits[0][0])
        self.assertIn("အသံတင်ဆက်ပုံစံကို", query.edits[0][0])
        self.assertRegex(query.edits[0][0], r"[\u1000-\u109F]")

    async def test_invalid_voice_and_style_callbacks_do_not_change_settings_or_preview(self):
        preview = AsyncMock()
        context = SimpleNamespace(user_data={})
        with patch.object(bot, "current_voice", "Kore"), patch.object(
            bot, "current_style", "dramatic_emotional"
        ), patch.object(bot, "send_voice_preview", preview):
            for data in ("voice:Unknown", "voice:Kore:extra", "style:dramatic_emotional:extra"):
                query = FakeCallback(data)
                await bot.callback_handler(owner_update(callback=query), context)
                self.assertEqual(query.edits[-1][0], "ရွေးချယ်မှု မမှန်ကန်ပါ။ မီနူးကို ပြန်ဖွင့်ပြီး ထပ်ရွေးပါ။")
            self.assertEqual(bot.current_voice, "Kore")
            self.assertEqual(bot.current_style, "dramatic_emotional")
        preview.assert_not_awaited()

    async def test_cancel_clone_removes_only_clone_session_state(self):
        with tempfile.TemporaryDirectory(prefix="ui-clone-isolation-") as temp:
            clone_dir = Path(temp) / "sample"
            clone_dir.mkdir()
            sample = clone_dir / "reference.wav"
            sample.write_bytes(b"temporary sample")
            user_data = {
                "voice_clone_sample_path": str(sample),
                "voice_clone_pending_audio": True,
                "voice_clone_transcript": "private transcript",
            }
            message = FakeMessage()
            update = owner_update(message=message)
            with patch.object(bot, "current_voice", "Kore"), patch.object(
                bot, "current_style", "fast_paced_recap"
            ):
                await bot.cancelclone_command(update, SimpleNamespace(user_data=user_data))
                self.assertEqual(bot.current_voice, "Kore")
                self.assertEqual(bot.current_style, "fast_paced_recap")
            self.assertEqual(user_data, {})
            self.assertFalse(clone_dir.exists())
            self.assertIn("Gemini TTS", message.replies[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
