import base64
import asyncio
import json
import logging
import os
import re
import subprocess
import shutil
import tempfile
import threading
import time
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
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash-lite-tts")
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "4000"))
TTS_INTER_SEGMENT_DELAY_SECONDS = float(os.getenv("TTS_INTER_SEGMENT_DELAY_SECONDS", "1.0"))
TTS_FALLBACK_RETRIES = max(1, int(os.getenv("TTS_FALLBACK_RETRIES", "2")))
TTS_RETRY_BACKOFF_SECONDS = float(os.getenv("TTS_RETRY_BACKOFF_SECONDS", "2.0"))
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
    "very_slow": "very slow, deliberate pace",
    "normal": "natural conversational pace",
    "fast": "brisk, energetic pace",
    "very_fast": "very fast, energetic pace",
}
SPEED_LABELS = {
    "very_slow": "0.75x Very Slow",
    "normal": "1.0x Normal",
    "fast": "1.25x Fast",
    "very_fast": "1.5x Very Fast",
}
STYLE_CATALOG = [
    ("suspense", "Suspense & Thriller", "Speak like a tense Burmese movie narrator building suspense and anticipation."),
    ("dramatic", "Dramatic & Emotional", "Speak like a dramatic Burmese movie narrator with deep emotion and expressive feeling."),
    ("recap", "Fast-Paced Recap", "Speak like a fast, clear Burmese movie recap narrator with energetic pacing."),
    ("action", "Action & Energetic", "Speak like an action movie narrator in Burmese: powerful, urgent, and energetic."),
    ("funny", "Funny & Conversational", "Speak like a funny, friendly Burmese storyteller having a lively conversation."),
    ("horror", "Dark & Horror", "Speak like a dark Burmese horror narrator: eerie, low, tense, and unsettling."),
    ("street", "Local Burmese Street-Talk", "Speak naturally like a local Burmese storyteller using relaxed, authentic street-talk."),
    ("trailer", "Epic & Cinematic Trailer", "Speak like an epic Burmese movie trailer narrator with grand cinematic intensity."),
    ("nostalgic", "Soft & Sad / Nostalgic", "Speak softly like a sad, nostalgic Burmese movie narrator with tender emotion."),
    ("satirical", "Satirical & Sarcastic", "Speak like a witty Burmese narrator using playful satire and dry sarcasm."),
]
STYLE_NAMES = {key: label for key, label, _ in STYLE_CATALOG}
STYLE_PROMPTS = {key: prompt for key, _, prompt in STYLE_CATALOG}
VOICE_MODES = {
    "single": "👤 Single Speaker",
    "multi": "👥 Multi-Speaker",
}
VOICE_MODE_DESCRIPTIONS = {
    "single": "အသံတစ်သံတည်းဖြင့် စာသားတစ်ခုလုံးကို Narrator အဖြစ် ဖတ်ပေးမည်။",
    "multi": "Speaker/Narrator/Character အလိုက် အလိုအလျောက်ခွဲပြီး အသံများပြောင်းကာ ဇာတ်ညွှန်းတစ်ခုလုံးကို MP3 တစ်ဖိုင်အဖြစ် ပေါင်းပေးမည်။",
}
STYLE_DESCRIPTIONS = {
    "suspense": "💡 သင့်တော်သည့် ဇာတ်လမ်း: သဲထိတ်ရင်ဖို၊ စုံထောက်၊ မှုခင်းဇာတ်လမ်းများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Puck သို့မဟုတ် Charon",
    "dramatic": "💡 သင့်တော်သည့် ဇာတ်လမ်း: အချစ်၊ မိသားစု၊ ဘဝဇာတ်ကြောင်း ဒရာမာများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Kore သို့မဟုတ် Leda",
    "recap": "💡 သင့်တော်သည့် ဇာတ်လမ်း: Shorts, TikTok, Facebook Reels, 5 Min Recaps\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Achird သို့မဟုတ် Fenrir",
    "action": "💡 သင့်တော်သည့် ဇာတ်လမ်း: အက်ရှင်၊ သိုင်းဇာတ်လမ်း၊ စူပါဟီးရိုးများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Fenrir သို့မဟုတ် Orion",
    "funny": "💡 သင့်တော်သည့် ဇာတ်လမ်း: ဟာသ၊ အန်နီမေးရှင်း ကာတွန်း၊ ပေါ့ပေါ့ပါးပါး ဇာတ်လမ်းများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Puck သို့မဟုတ် Achird",
    "horror": "💡 သင့်တော်သည့် ဇာတ်လမ်း: သရဲကား၊ စိတ္တဇ လူသတ်သမား၊ ခြောက်ခြားဖွယ် ဇာတ်လမ်းများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Charon",
    "street": "💡 သင့်တော်သည့် ဇာတ်လမ်း: TikTok/Facebook Short Video၊ လူငယ်ကြိုက် ဇာတ်လမ်းများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Achird သို့မဟုတ် Puck",
    "trailer": "💡 သင့်တော်သည့် ဇာတ်လမ်း: ရုပ်ရှင် ကြော်ငြာအသံ၊ Sci-Fi၊ သမိုင်းဝင် ကာတွန်း/ရုပ်ရှင်များ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Orion သို့မဟုတ် Charon",
    "nostalgic": "💡 သင့်တော်သည့် ဇာတ်လမ်း: ဝမ်းနည်းဖွယ် အချစ်၊ မိသားစု ဒရာမာများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Leda သို့မဟုတ် Kore",
    "satirical": "💡 သင့်တော်သည့် ဇာတ်လမ်း: Movie Mistakes Review၊ စောင်းမြောင်း ရယ်စရာ ဇာတ်လမ်းများ။\n🎙️ အလိုက်ဖက်ဆုံး အသံ: Puck သို့မဟုတ် Achird",
}
VOICE_DESCRIPTIONS = {
    "Kore": "💡 အချစ်၊ မိသားစု၊ ဒရာမာနှင့် ဝမ်းနည်းဖွယ် ဇာတ်လမ်းများအတွက် အထူးကောင်းမွန်သည်။",
    "Leda": "💡 အေးဆေးညင်သာသော ဇာတ်လမ်းများ၊ ပုံပြင်များနှင့် အလွမ်းကားများအတွက် သင့်တော်သည်။",
    "Puck": "💡 စုံထောက်၊ သဲထိတ်ရင်ဖို၊ ဟာသနှင့် ပေါ့ပေါ့ပါးပါး Movie Review များအတွက် အထူးကောင်းမွန်သည်။",
    "Charon": "💡 သရဲကား၊ စိတ္တဇ လူသတ်သမားနှင့် Horror ဇာတ်လမ်းများအတွက် အနက်သံ အကောင်းဆုံးဖြစ်သည်။",
    "Fenrir": "💡 အက်ရှင်၊ TikTok Shorts နှင့် မြန်မြန်ဆန်ဆန် ဇာတ်လမ်းပြောပြရန် သင့်တော်သည်။",
    "Achird": "💡 အက်ရှင်၊ TikTok Shorts နှင့် မြန်မြန်ဆန်ဆန် ဇာတ်လမ်းပြောပြရန် သင့်တော်သည်။",
    "Orion": "💡 Superhero ကားများနှင့် ရုပ်ရှင် ကြော်ငြာအသံ (Trailer Style) အတွက် အသံဩဇာ အပြည့်ပါသည်။",
}
FEMALE_VOICE_FALLBACK = "💡 အချစ်၊ မိသားစု၊ ဒရာမာ၊ ပုံပြင်များနှင့် အေးဆေးညင်သာသော ဇာတ်လမ်းများအတွက် သင့်တော်ပါသည်။"
MALE_VOICE_FALLBACK = "💡 အက်ရှင်၊ သဲထိတ်ရင်ဖို၊ စုံထောက်၊ TikTok/Reels Shorts နှင့် Movie Recaps များအတွက် သင့်တော်ပါသည်။"
current_voice = DEFAULT_VOICE
current_speed = os.getenv("DEFAULT_SPEED", "normal")
if current_speed not in SPEEDS:
    current_speed = "normal"
current_style = os.getenv("DEFAULT_STYLE", "trailer")
if current_style not in STYLE_NAMES:
    current_style = "trailer"
current_voice_mode = os.getenv("VOICE_MODE", "multi")
if current_voice_mode not in VOICE_MODES:
    current_voice_mode = "multi"


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED_TELEGRAM_USER_IDS)


async def reject_unauthorized(update: Update) -> None:
    if update.message:
        await update.message.reply_text("Sorry, this bot is private and only available to authorized users.")


def voice_keyboard(pending_index: int | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for index, (name, gender) in enumerate(VOICE_CATALOG):
        marker = "⏳ " if pending_index == index else "✓ " if name == current_voice else ""
        label = f"{marker}{name} ({gender[0]})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"voice:{index}"))
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    if pending_index is not None:
        rows.append([InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:voice:{pending_index}")])
    rows.append([InlineKeyboardButton("↩ Back to main menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def voice_description(name: str, gender: str) -> str:
    if name in VOICE_DESCRIPTIONS:
        return VOICE_DESCRIPTIONS[name]
    return FEMALE_VOICE_FALLBACK if gender == "Female" else MALE_VOICE_FALLBACK


def speed_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"{'✓ ' if current_speed == key else ''}{SPEED_LABELS[key]}",
            callback_data=f"speed:{key}",
        )
        for key in SPEEDS
    ]
    return InlineKeyboardMarkup([[button] for button in buttons] + [[
        InlineKeyboardButton("↩ Back to main menu", callback_data="menu:home")
    ]])


def style_keyboard(pending_key: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"{'⏳ ' if pending_key == key else '✓ ' if current_style == key else ''}{label}",
            callback_data=f"style:{key}",
        )
        for key, label, _ in STYLE_CATALOG
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    if pending_key is not None:
        rows.append([InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:style:{pending_key}")])
    rows.append([InlineKeyboardButton("↩ Back to main menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def voice_mode_keyboard(pending_mode: str | None = None) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            f"{'⏳ ' if pending_mode == key else '✓ ' if current_voice_mode == key else ''}{label}",
            callback_data=f"mode:{key}",
        )
    ] for key, label in VOICE_MODES.items()]
    if pending_mode is not None:
        rows.append([InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:mode:{pending_mode}")])
    rows.append([InlineKeyboardButton("↩ Back to main menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def confirmation_keyboard(kind: str, value: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm / Select", callback_data=f"confirm:{kind}:{value}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data=f"back:{kind}")],
    ])


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙 Select Voice", callback_data="menu:voice")],
        [InlineKeyboardButton("🗣 Select Voice Mode", callback_data="menu:mode")],
        [InlineKeyboardButton("⚡ Select Speed", callback_data="menu:speed")],
        [InlineKeyboardButton("🎬 Select Movie Style", callback_data="menu:style")],
    ])


def welcome_text() -> str:
    return (
        "Welcome to the private Gemini TTS bot.\n\n"
        f"Current voice: {current_voice}\n"
        f"Voice mode: {VOICE_MODES[current_voice_mode]}\n"
        f"Current speed: {SPEED_LABELS[current_speed]}\n\n"
        f"Current style: {STYLE_NAMES[current_style]}\n\n"
        "Choose a setting below, then send me any text."
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


class GeminiTTSAPIError(RuntimeError):
    def __init__(self, status_code: int, model: str, body: str):
        self.status_code = status_code
        self.model = model
        self.body = body
        super().__init__(f"Gemini TTS API error {status_code} for {model}: {body[:1000]}")


def _generate_audio_base64(text: str, voice: str, model: str, speed: str, style: str) -> str:
    delivery = f"{STYLE_PROMPTS[style]}; {SPEEDS[speed]}"
    payload = {
        "model": model,
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
        raise GeminiTTSAPIError(response.status_code, model, response.text)
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Gemini returned non-JSON response for {model}: {response.text[:1000]}") from exc
    audio_b64 = extract_audio_base64(response_payload)
    if not audio_b64:
        raise RuntimeError(f"Gemini returned no audio data for {model}: {response.text[:1000]}")
    return audio_b64


def synthesize_speech(text: str, voice: str, speed: str, style: str = "trailer") -> Path:
    """Generate audio with per-turn primary/lite fallback and transient retries."""
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL not in models:
        models.append(GEMINI_FALLBACK_MODEL)
    audio_b64 = None
    last_api_error = None
    retryable_statuses = {429, 500, 502, 503, 504}
    for model_index, model in enumerate(models):
        # The primary gets one attempt before the lite fallback is invoked.
        # The fallback gets bounded retries so one transient response does not
        # abort the entire combined multi-speaker file.
        max_attempts = TTS_FALLBACK_RETRIES if model_index else 1
        for attempt in range(1, max_attempts + 1):
            try:
                audio_b64 = _generate_audio_base64(text, voice, model, speed, style)
                if model_index:
                    logger.info(
                        "Gemini TTS fallback succeeded for speaker turn: model=%s voice=%s attempt=%s",
                        model, voice, attempt,
                    )
                break
            except GeminiTTSAPIError as exc:
                last_api_error = exc
                logger.error(
                    "Gemini TTS request failed: model=%s status=%s voice=%s attempt=%s/%s body=%s",
                    exc.model, exc.status_code, voice, attempt, max_attempts, exc.body[:2000],
                )
                if model_index == 0 and exc.status_code in retryable_statuses and len(models) > 1:
                    logger.warning(
                        "Primary model rate-limited/unavailable for this speaker turn; "
                        "invoking fallback model=%s",
                        GEMINI_FALLBACK_MODEL,
                    )
                    break
                if exc.status_code not in retryable_statuses or attempt >= max_attempts:
                    raise
                delay = TTS_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("Retrying fallback model after %.1fs for speaker turn", delay)
                time.sleep(delay)
        if audio_b64:
            break
    if not audio_b64:
        if last_api_error:
            raise last_api_error
        raise RuntimeError("Gemini returned no audio data")

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


def convert_to_voice_note(mp3_path: Path) -> Path:
    """Convert a generated MP3 preview to Telegram Voice Note OGG/Opus."""
    ogg_path = mp3_path.parent / "preview.ogg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3_path),
                "-c:a", "libopus", "-b:a", "64k", "-vbr", "on", "-vn", str(ogg_path),
            ],
            check=True,
            timeout=90,
        )
        if not ogg_path.exists() or ogg_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty OGG voice note")
        return ogg_path
    except Exception:
        ogg_path.unlink(missing_ok=True)
        raise


def tts_failure_message(exc: Exception) -> str:
    if isinstance(exc, GeminiTTSAPIError):
        if exc.status_code == 429:
            return (
                "Google Gemini TTS quota/rate limit reached for this API project. "
                "Please wait for the quota reset or enable a paid Gemini tier."
            )
        if exc.status_code in {500, 502, 503, 504}:
            return "Google Gemini TTS is temporarily busy. Please try again in a few minutes."
        if exc.status_code in {401, 403}:
            return "The Gemini API key is invalid or lacks access to the selected TTS model."
    return "I could not generate audio right now. Please try again later."


def progress_bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, round(percent * width / 100)))
    return f"[{'█' * filled}{'░' * (width - filled)}] {percent}%"


async def animate_progress(status_message, prefix: str = "Generating audio") -> None:
    """Keep Telegram status visibly moving while the blocking TTS request runs."""
    stages = (15, 25, 35, 45, 55, 65, 75, 82)
    try:
        for percent in stages:
            await asyncio.sleep(2.5)
            await status_message.edit_text(f"{prefix}... {progress_bar(percent)}")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Unable to update Telegram progress message", exc_info=True)


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


SPEAKER_LABEL_RE = re.compile(
    # Any non-empty label before a line-leading colon is metadata only. This
    # intentionally supports Burmese names such as ရှာနန်းဝေး, စနစ်, and ဂျန်ဂျုန်း.
    r"^\s*(?:\[\s*)?(?P<label>[^:：\[\]\n]{1,60})(?:\s*\])?\s*[:：]\s*(?P<text>.+)$",
    re.IGNORECASE,
)


def parse_script(text: str) -> list[tuple[str, str]]:
    """Parse labels as routing metadata; never include the labels in spoken text."""
    segments: list[tuple[str, str]] = []
    current_label = "Narrator"
    current_lines: list[str] = []

    def flush() -> None:
        content = " ".join(line.strip() for line in current_lines if line.strip()).strip()
        if content:
            segments.append((current_label, content))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            current_lines.clear()
            continue
        match = SPEAKER_LABEL_RE.match(line)
        if match:
            flush()
            current_lines.clear()
            current_label = re.sub(r"\s+", " ", match.group("label")).strip()
            # Only the captured dialogue text is retained; the label and its
            # separator are deliberately discarded before TTS synthesis.
            current_lines.append(match.group("text").strip())
        else:
            current_lines.append(line)
    flush()

    if not segments:
        return [("Narrator", text.strip())]
    return segments


def voice_for_speaker(label: str, speaker_order: list[str]) -> str:
    """Choose a stable voice profile from role/name hints and speaker order."""
    normalized = label.lower()
    if normalized in {"narrator", "host"}:
        return current_voice
    female_hints = ("female", "woman", "girl", "mother", "wife", "မ", "မိန်း", "အမေ")
    male_hints = ("male", "man", "boy", "father", "husband", "ကျွန်တော်", "အမျိုးသား")
    if any(hint in normalized for hint in female_hints):
        return "Kore"
    if any(hint in normalized for hint in male_hints):
        return "Puck"
    dialogue_speakers = [speaker for speaker in speaker_order if speaker.lower() not in {"narrator", "host"}]
    ordinal = dialogue_speakers.index(label) if label in dialogue_speakers else len(dialogue_speakers)
    if ordinal % 2 == 0:
        return "Puck"
    return "Kore"


def expand_script_segments(text: str) -> list[tuple[str, str, str]]:
    """Return (speaker, text, assigned voice), splitting oversized blocks safely."""
    parsed = parse_script(text)
    speaker_order: list[str] = []
    for speaker, _ in parsed:
        if speaker not in speaker_order:
            speaker_order.append(speaker)
    expanded: list[tuple[str, str, str]] = []
    for speaker, block in parsed:
        assigned_voice = voice_for_speaker(speaker, speaker_order)
        for chunk in split_text_for_tts(block):
            expanded.append((speaker, chunk, assigned_voice))
    return expanded


def compile_script_to_mp3(segments: list[tuple[str, str, str]], speed: str, style: str) -> Path:
    """Synthesize all speaker segments and concatenate them into one MP3."""
    master_dir = Path(tempfile.mkdtemp(prefix="telegram-gemini-script-"))
    segment_paths: list[Path] = []
    try:
        for index, (_, segment_text, assigned_voice) in enumerate(segments, start=1):
            if index > 1 and TTS_INTER_SEGMENT_DELAY_SECONDS > 0:
                logger.info(
                    "Rate-limit pacing: waiting %.2fs before speaker turn %s/%s",
                    TTS_INTER_SEGMENT_DELAY_SECONDS, index, len(segments),
                )
                time.sleep(TTS_INTER_SEGMENT_DELAY_SECONDS)
            logger.info(
                "Synthesizing speaker turn %s/%s with voice=%s; fallback=%s",
                index, len(segments), assigned_voice, GEMINI_FALLBACK_MODEL,
            )
            generated = synthesize_speech(segment_text, assigned_voice, speed, style)
            target = master_dir / f"segment_{index:04d}.mp3"
            shutil.copy2(generated, target)
            cleanup_audio(generated)
            segment_paths.append(target)
        if not segment_paths:
            raise ValueError("No speech segments were detected")
        concat_list = master_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{path.name}'" for path in segment_paths) + "\n", encoding="utf-8")
        final_path = master_dir / "voiceover.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c:a", "libmp3lame", "-b:a", "192k", "-vn", str(final_path)],
            check=True,
            timeout=max(120, 30 * len(segment_paths)),
            cwd=master_dir,
        )
        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty combined MP3 file")
        for path in segment_paths:
            path.unlink(missing_ok=True)
        concat_list.unlink(missing_ok=True)
        return final_path
    except Exception:
        shutil.rmtree(master_dir, ignore_errors=True)
        raise


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        await update.message.reply_text(welcome_text(), reply_markup=start_keyboard())


async def setvoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        current_gender = next(gender for name, gender in VOICE_CATALOG if name == current_voice)
        await update.message.reply_text(
            f"Current voice: {current_voice} ({current_gender})\n{voice_description(current_voice, current_gender)}\n\n"
            f"Choose one of the {len(VOICE_CATALOG)} Gemini voices:",
            reply_markup=voice_keyboard(),
        )


async def setspeed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
    elif update.message:
        await update.message.reply_text(
            f"Current speed: {SPEED_LABELS[current_speed]}\nChoose a speed:",
            reply_markup=speed_keyboard(),
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_voice, current_speed, current_style, current_voice_mode
    query = update.callback_query
    if not query or not is_owner(update):
        return
    await query.answer()
    kind, value = query.data.split(":", 1)
    if kind == "confirm":
        confirm_kind, confirm_value = value.split(":", 1)
        if confirm_kind == "voice":
            index = int(confirm_value)
            if context.user_data.get("pending_voice_index") != index or not 0 <= index < len(VOICE_CATALOG):
                await query.edit_message_text("This voice preview has expired. Please choose a voice again.", reply_markup=voice_keyboard())
                return
            current_voice = VOICE_CATALOG[index][0]
            context.user_data.pop("pending_voice_index", None)
            await query.edit_message_text(
                f"✅ Voice selected: {current_voice}\n{voice_description(current_voice, VOICE_CATALOG[index][1])}",
                reply_markup=start_keyboard(),
            )
        elif confirm_kind == "style":
            if context.user_data.get("pending_style_key") != confirm_value or confirm_value not in STYLE_NAMES:
                await query.edit_message_text("This style preview has expired. Please choose a style again.", reply_markup=style_keyboard())
                return
            current_style = confirm_value
            context.user_data.pop("pending_style_key", None)
            await query.edit_message_text(
                f"✅ Movie style selected: {STYLE_NAMES[current_style]}\n\n{STYLE_DESCRIPTIONS[current_style]}",
                reply_markup=start_keyboard(),
            )
        elif confirm_kind == "mode":
            if context.user_data.get("pending_voice_mode") != confirm_value or confirm_value not in VOICE_MODES:
                await query.edit_message_text(
                    "This Voice Mode selection has expired. Please choose again.",
                    reply_markup=voice_mode_keyboard(),
                )
                return
            current_voice_mode = confirm_value
            context.user_data.pop("pending_voice_mode", None)
            await query.edit_message_text(
                f"✅ Voice Mode selected: {VOICE_MODES[current_voice_mode]}\n{VOICE_MODE_DESCRIPTIONS[current_voice_mode]}",
                reply_markup=start_keyboard(),
            )
    elif kind == "back":
        context.user_data.pop("pending_voice_index", None)
        context.user_data.pop("pending_style_key", None)
        if value == "voice":
            current_gender = next(gender for name, gender in VOICE_CATALOG if name == current_voice)
            await query.edit_message_text(
                f"Current voice: {current_voice} ({current_gender})\n{voice_description(current_voice, current_gender)}\n\nChoose a Gemini voice:",
                reply_markup=voice_keyboard(),
            )
        elif value == "style":
            await query.edit_message_text(
                f"Current movie style: {STYLE_NAMES[current_style]}\n{STYLE_DESCRIPTIONS[current_style]}\n\nChoose a voiceover style:",
                reply_markup=style_keyboard(),
            )
    elif kind == "menu" and value == "home":
        context.user_data.pop("pending_voice_index", None)
        context.user_data.pop("pending_style_key", None)
        context.user_data.pop("pending_voice_mode", None)
        await query.edit_message_text(welcome_text(), reply_markup=start_keyboard())
    elif kind == "menu" and value == "voice":
        current_gender = next(gender for name, gender in VOICE_CATALOG if name == current_voice)
        await query.edit_message_text(
            f"Current voice: {current_voice} ({current_gender})\n{voice_description(current_voice, current_gender)}\n\n"
            "Choose a Gemini voice:",
            reply_markup=voice_keyboard(),
        )
    elif kind == "menu" and value == "speed":
        await query.edit_message_text(
            f"Current speed: {SPEED_LABELS[current_speed]}\nChoose a speech speed:",
            reply_markup=speed_keyboard(),
        )
    elif kind == "menu" and value == "mode":
        await query.edit_message_text(
            f"Current Voice Mode: {VOICE_MODES[current_voice_mode]}\n{VOICE_MODE_DESCRIPTIONS[current_voice_mode]}\n\n"
            "Choose how the script should be spoken:",
            reply_markup=voice_mode_keyboard(),
        )
    elif kind == "menu" and value == "style":
        await query.edit_message_text(
            f"Current movie style: {STYLE_NAMES[current_style]}\n{STYLE_DESCRIPTIONS[current_style]}\n\n"
            "Choose a voiceover style:",
            reply_markup=style_keyboard(),
        )
    elif kind == "voice":
        index = int(value)
        if index < 0 or index >= len(VOICE_CATALOG):
            await query.edit_message_text("Unknown voice selection.")
            return
        preview_voice, gender = VOICE_CATALOG[index]
        context.user_data["pending_voice_index"] = index
        description = voice_description(preview_voice, gender)
        await query.edit_message_text(
            f"Selected: {preview_voice} ({gender}) — Pending Confirmation\n"
            f"{description}\n\n"
            "Preview audio is being generated. Use ✅ Confirm to apply this voice.",
            reply_markup=voice_keyboard(index),
        )
        speaker = "ကျွန်တော်" if gender == "Male" else "ကျွန်မ"
        preview_text = f"မင်္ဂလာပါ၊ {speaker}ကတော့ {preview_voice} ဖြစ်ပါတယ်။"
        audio_path = None
        voice_note_path = None
        try:
            await query.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            audio_path = await __import__("asyncio").to_thread(
                synthesize_speech, preview_text, preview_voice, current_speed, current_style
            )
            voice_note_path = await __import__("asyncio").to_thread(convert_to_voice_note, audio_path)
            with voice_note_path.open("rb") as voice_file:
                await query.message.reply_voice(voice=voice_file)
        except Exception as exc:
            logger.exception("Voice preview generation failed for %s", preview_voice)
            await query.message.reply_text(f"Preview for {preview_voice} failed: {tts_failure_message(exc)}")
        finally:
            if voice_note_path:
                cleanup_audio(voice_note_path)
            elif audio_path:
                cleanup_audio(audio_path)
    elif kind == "speed" and value in SPEEDS:
        current_speed = value
        await query.edit_message_text(
            f"Speed changed to {SPEED_LABELS[value]}.",
            reply_markup=start_keyboard(),
        )
    elif kind == "style" and value in STYLE_NAMES:
        context.user_data["pending_style_key"] = value
        await query.edit_message_text(
            f"Selected: {STYLE_NAMES[value]} — Pending Confirmation\n\n"
            f"{STYLE_DESCRIPTIONS[value]}\n\n"
            "Generating a style preview voice note. Use ✅ Confirm to apply this movie style.",
            reply_markup=style_keyboard(value),
        )
        style_preview_text = f"မင်္ဂလာပါ။ ဒီအသံပုံစံက {STYLE_NAMES[value]} ဖြစ်ပါတယ်။"
        audio_path = None
        voice_note_path = None
        try:
            await query.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            audio_path = await __import__("asyncio").to_thread(
                synthesize_speech, style_preview_text, current_voice, current_speed, value
            )
            voice_note_path = await __import__("asyncio").to_thread(convert_to_voice_note, audio_path)
            with voice_note_path.open("rb") as voice_file:
                await query.message.reply_voice(voice=voice_file)
        except Exception as exc:
            logger.exception("Style preview generation failed for %s", value)
            await query.message.reply_text(f"Preview for {STYLE_NAMES[value]} failed: {tts_failure_message(exc)}")
        finally:
            if voice_note_path:
                cleanup_audio(voice_note_path)
            elif audio_path:
                cleanup_audio(audio_path)
    elif kind == "mode" and value in VOICE_MODES:
        context.user_data["pending_voice_mode"] = value
        await query.edit_message_text(
            f"Selected: {VOICE_MODES[value]} — Pending Confirmation\n\n"
            f"{VOICE_MODE_DESCRIPTIONS[value]}\n\n"
            "Use ✅ Confirm to apply this Voice Mode.",
            reply_markup=voice_mode_keyboard(value),
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text:
        return
    if current_voice_mode == "single":
        segments = [("Narrator", chunk, current_voice) for chunk in split_text_for_tts(text)]
        mode_label = VOICE_MODES["single"]
    else:
        segments = expand_script_segments(text)
        mode_label = VOICE_MODES["multi"]
    speaker_count = len({speaker for speaker, _, _ in segments})
    status_message = await update.message.reply_text(
        f"{mode_label}: analyzing {speaker_count} role(s) and generating one combined MP3... {progress_bar(5)}"
    )
    progress_task = asyncio.create_task(animate_progress(status_message, "Generating combined audio"))
    audio_path = None
    try:
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        logger.info(
            "Voice mode=%s segments=%s speakers=%s assignments=%s",
            current_voice_mode,
            len(segments), speaker_count, [(speaker, voice) for speaker, _, voice in segments],
        )
        audio_path = await asyncio.to_thread(
            compile_script_to_mp3, segments, current_speed, current_style
        )
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)
        await status_message.edit_text(
            f"Combining {len(segments)} speaker segment(s) into one MP3... {progress_bar(90)}"
        )
        with audio_path.open("rb") as audio_file:
            await update.message.reply_document(
                document=audio_file,
                filename="voiceover.mp3",
                caption=f"Combined {mode_label} voiceover ({speaker_count} role(s))",
            )
        await status_message.edit_text(f"Voiceover ready... {progress_bar(100)}")
    except Exception as exc:
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)
        logger.exception("Combined multi-speaker Gemini TTS generation failed")
        await status_message.edit_text(
            f"Combined audio generation failed... {progress_bar(100)}\n{tts_failure_message(exc)}"
        )
    finally:
        if audio_path:
            shutil.rmtree(audio_path.parent, ignore_errors=True)


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
    application.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^(menu|voice|speed|style|mode|confirm|back):"))
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
