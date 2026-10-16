import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-gemini-tts-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
_allowed_ids = os.getenv("ALLOWED_TELEGRAM_USER_IDS", os.getenv("TELEGRAM_USER_ID", ""))
ALLOWED_TELEGRAM_USER_IDS = {
    int(value.strip()) for value in _allowed_ids.split(",") if value.strip().isdigit()
}
if not ALLOWED_TELEGRAM_USER_IDS:
    raise ValueError("Configure at least one Telegram user ID")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash-tts")
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "4000"))
PORT = int(os.getenv("PORT", "10000"))
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Official Gemini prebuilt TTS catalog: 30 documented voices.
# The catalog is intentionally included locally so /setvoice works without a catalog API call.
VOICE_CATALOG = [
    ("Achernar", "Female"),
    ("Achird", "Male"),
    ("Algenib", "Male"),
    ("Algieba", "Male"),
    ("Alnilam", "Male"),
    ("Aoede", "Female"),
    ("Autonoe", "Female"),
    ("Callirrhoe", "Female"),
    ("Charon", "Male"),
    ("Despina", "Female"),
    ("Enceladus", "Male"),
    ("Erinome", "Female"),
    ("Fenrir", "Male"),
    ("Gacrux", "Female"),
    ("Iapetus", "Male"),
    ("Kore", "Female"),
    ("Laomedeia", "Female"),
    ("Leda", "Female"),
    ("Orus", "Male"),
    ("Pulcherrima", "Female"),
    ("Puck", "Male"),
    ("Rasalgethi", "Male"),
    ("Sadachbia", "Male"),
    ("Sadaltager", "Male"),
    ("Schedar", "Male"),
    ("Sulafat", "Female"),
    ("Umbriel", "Male"),
    ("Vindemiatrix", "Female"),
    ("Zephyr", "Female"),
    ("Zubenelgenubi", "Male"),
]
VOICE_NAMES = {name for name, _ in VOICE_CATALOG}
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "Kore")
if DEFAULT_VOICE not in VOICE_NAMES:
    DEFAULT_VOICE = "Kore"

SPEEDS = {
    "slow": "slow, deliberate pace",
    "normal": "natural conversational pace",
    "fast": "brisk, energetic pace",
}
current_voice = DEFAULT_VOICE
current_speed = os.getenv("DEFAULT_SPEED", "normal")
if current_speed not in SPEEDS:
    current_speed = "normal"
FIXED_STYLE = "Professional cinematic movie storyteller style, natural and expressive tone"


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED_TELEGRAM_USER_IDS)


async def reject_unauthorized(update: Update) -> None:
    if update.message:
        await update.message.reply_text("Sorry, this bot is private and only available to authorized users.")


def voice_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for index, (name, gender) in enumerate(VOICE_CATALOG):
        label = f"{'✓ ' if name == current_voice else ''}{name} ({gender[0]})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"voice:{index}"))
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


def speed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"{'✓ ' if current_speed == key else ''}{key.title()}", callback_data=f"speed:{key}")
            for key in SPEEDS
        ]]
    )


def extract_audio_base64(payload: dict) -> str | None:
    output_audio = payload.get("output_audio")
    if isinstance(output_audio, dict) and output_audio.get("data"):
        return output_audio["data"]
    for step in reversed(payload.get("steps", [])):
        if step.get("type") != "model_output":
            continue
        for content in reversed(step.get("content", [])):
            if content.get("type") == "audio" and content.get("data"):
                return content["data"]
    return None


def synthesize_speech(text: str, voice: str, speed: str) -> Path:
    """Generate Gemini TTS WAV audio and convert it to a downloadable MP3 file."""
    delivery = f"{FIXED_STYLE}; {SPEEDS[speed]}"
    payload = {
        "model": GEMINI_MODEL,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": delivery,
                }],
            }],
        }],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    response = requests.post(
        GEMINI_INTERACTIONS_URL,
        headers={"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"},
        json=payload,
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"Gemini TTS API error {response.status_code}: {response.text[:1000]}")
    audio_b64 = extract_audio_base64(response.json())
    if not audio_b64:
        raise RuntimeError(f"Gemini returned no audio data: {response.text[:1000]}")

    temp_dir = Path(tempfile.mkdtemp(prefix="telegram-gemini-tts-"))
    wav_path = temp_dir / "speech.wav"
    mp3_path = temp_dir / "speech.mp3"
    try:
        wav_path.write_bytes(base64.b64decode(audio_b64))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", "192k", "-vn", str(mp3_path)],
            check=True,
            timeout=90,
        )
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty MP3 file")
        wav_path.unlink(missing_ok=True)
        return mp3_path
    except Exception:
        for path in (wav_path, mp3_path):
            path.unlink(missing_ok=True)
        temp_dir.rmdir()
        raise


def cleanup_audio(audio_path: Path) -> None:
    audio_path.unlink(missing_ok=True)
    for leftover in audio_path.parent.iterdir():
        leftover.unlink(missing_ok=True)
    audio_path.parent.rmdir()


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal Render health endpoint; Telegram polling remains in the main thread."""

    def _health_body(self) -> bytes:
        return json.dumps({"status": "ok", "service": "telegram-gemini-tts"}).encode()

    def do_GET(self) -> None:
        if self.path.rstrip("/") not in ("", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = self._health_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if self.path.rstrip("/") not in ("", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = self._health_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        logger.info("health-server: " + format, *args)


def start_health_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health endpoint listening on 0.0.0.0:%s/health", PORT)
    return server


def split_text_for_tts(text: str, max_length: int = MAX_TEXT_LENGTH) -> list[str]:
    """Split long text at paragraph/sentence/word boundaries without exceeding the TTS limit."""
    if len(text) <= max_length:
        return [text]

    pieces = re.split(r"(?<=[.!?။！？])\s+|\n+", text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        candidate = f"{current} {piece}".strip() if current else piece
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(piece) > max_length:
            cut = piece.rfind(" ", 0, max_length + 1)
            if cut <= 0:
                cut = max_length
            chunks.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        current = piece
    if current:
        chunks.append(current)
    return chunks


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        await update.message.reply_text(
            f"Private Gemini TTS bot ready. Voice: {current_voice}; speed: {current_speed}.\n"
            "Use /setvoice or /setspeed."
        )


async def setvoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        await update.message.reply_text(
            f"Current voice: {current_voice}\nChoose one of the {len(VOICE_CATALOG)} Gemini voices:",
            reply_markup=voice_keyboard(),
        )


async def setspeed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        await update.message.reply_text(
            f"Current speed: {current_speed.title()}\nChoose a speed:",
            reply_markup=speed_keyboard(),
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_voice, current_speed
    query = update.callback_query
    if not query or not is_owner(update):
        return
    await query.answer()
    kind, value = query.data.split(":", 1)
    if kind == "voice":
        index = int(value)
        if index < 0 or index >= len(VOICE_CATALOG):
            await query.edit_message_text("Unknown voice selection.")
            return
        current_voice = VOICE_CATALOG[index][0]
        await query.edit_message_text(f"Voice changed to {current_voice}.")
    elif kind == "speed" and value in SPEEDS:
        current_speed = value
        await query.edit_message_text(f"Speed changed to {value.title()}.")
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text:
        return
    chunks = split_text_for_tts(text)
    if len(chunks) > 1:
        await update.message.reply_text(f"This text will be sent as {len(chunks)} voice notes.")

    for index, chunk in enumerate(chunks, start=1):
        # Telegram has no UPLOAD_AUDIO chat action; UPLOAD_DOCUMENT is the
        # supported generic action while the result is sent with reply_audio.
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        audio_path = None
        try:
            audio_path = await __import__("asyncio").to_thread(
                synthesize_speech, chunk, current_voice, current_speed
            )
            with audio_path.open("rb") as audio_file:
                await update.message.reply_audio(
                    audio=audio_file,
                    filename=f"gemini_story_{index}.mp3",
                    title=f"Gemini Story {index}",
                    performer="Gemini TTS",
                )
        except Exception:
            logger.exception("Gemini TTS voice generation failed for chunk %s/%s", index, len(chunks))
            await update.message.reply_text(
                f"I could not generate audio file {index} of {len(chunks)}. Please try again later."
            )
            break
        finally:
            if audio_path:
                cleanup_audio(audio_path)


async def handle_other_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        await update.message.reply_text("Send text, or use /setvoice or /setspeed.")


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setvoice", setvoice_command))
    application.add_handler(CommandHandler("setspeed", setspeed_command))
    application.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^(voice|speed):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_other_message))
    return application


if __name__ == "__main__":
    start_health_server()
    logger.info(
        "Starting Gemini TTS bot for %s authorized users: model=%s voice=%s speed=%s",
        len(ALLOWED_TELEGRAM_USER_IDS), GEMINI_MODEL, current_voice, current_speed,
    )
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
