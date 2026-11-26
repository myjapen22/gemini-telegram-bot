import base64
import asyncio
import json
import logging
import math
import threading
import msgpack
import os
import re
import subprocess
import shutil
import tempfile
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# HTTPX logs full Telegram API URLs at INFO, including the bot token in the URL path.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ALLOWED_TELEGRAM_USER_IDS = [
    int(uid.strip())
    for uid in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if uid.strip().isdigit()
]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash-tts").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash-lite-tts").strip()
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "Kore").strip()
DEFAULT_SPEED = os.getenv("DEFAULT_SPEED", "normal").strip()
DEFAULT_STYLE = os.getenv("DEFAULT_STYLE", "neutral").strip()
DEFAULT_VOICE_MODE = os.getenv("DEFAULT_VOICE_MODE", "single").strip()
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "4000"))
TTS_INTER_SEGMENT_DELAY_SECONDS = float(os.getenv("TTS_INTER_SEGMENT_DELAY_SECONDS", "1.0"))
TTS_FALLBACK_RETRIES = max(1, int(os.getenv("TTS_FALLBACK_RETRIES", "2")))
TTS_RETRY_BACKOFF_SECONDS = float(os.getenv("TTS_RETRY_BACKOFF_SECONDS", "2.0"))
PORT = int(os.getenv("PORT", "10000"))
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

VOICE_CLONE_API_KEY = (os.getenv("VOICE_CLONE_API_KEY") or os.getenv("FISH_API_KEY") or "").strip()
VOICE_CLONE_API_BASE_URL = (
    os.getenv("VOICE_CLONE_API_BASE_URL", "https://api.fish.audio").strip().rstrip("/")
    or "https://api.fish.audio"
)
VOICE_CLONE_TTS_MODEL = "s2.1-pro-free"
VOICE_CLONE_MAX_UPLOAD_MB = max(1, int(os.getenv("VOICE_CLONE_MAX_UPLOAD_MB", "12")))
VOICE_CLONE_MIN_AUDIO_SECONDS = max(1, int(os.getenv("VOICE_CLONE_MIN_AUDIO_SECONDS", "10")))
VOICE_CLONE_MAX_AUDIO_SECONDS = max(
    VOICE_CLONE_MIN_AUDIO_SECONDS, int(os.getenv("VOICE_CLONE_MAX_AUDIO_SECONDS", "60"))
)
VOICE_CLONE_SPEEDS = {"very_slow": 0.75, "normal": 1.0, "fast": 1.25, "very_fast": 1.5}
VOICE_CLONE_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".oga"}

VOICES = {
    "Puck": {"gender": "Male", "tone": "Upbeat", "description": "Enthusiastic and clear"},
    "Charon": {"gender": "Male", "tone": "Deep", "description": "Authoritative and calm"},
    "Kore": {"gender": "Female", "tone": "Relaxed", "description": "Warm and natural"},
    "Fenrir": {"gender": "Male", "tone": "Intense", "description": "Strong and energetic"},
    "Aoede": {"gender": "Female", "tone": "Melodic", "description": "Expressive and smooth"},
}

SPEEDS = {
    "very_slow": "Speak very slowly with clear pauses",
    "slow": "Speak slightly slower than normal speed",
    "normal": "Speak at a natural, standard conversational speed",
    "fast": "Speak slightly faster than normal speed",
    "very_fast": "Speak very fast and briskly",
}

SPEED_LABELS = {
    "very_slow": "Very Slow",
    "slow": "Slow",
    "normal": "Normal",
    "fast": "Fast",
    "very_fast": "Very Fast",
}

STYLES = {
    "neutral": "Read naturally without exaggerated emotion",
    "cheerful": "Read with a warm, cheerful, and upbeat tone",
    "dramatic": "Read with dramatic pacing, emotion, and emphasis",
    "news": "Read like a professional news broadcaster",
    "storytelling": "Read like a captivating narrator telling a story",
}

STYLE_NAMES = {
    "neutral": "Natural / Neutral",
    "cheerful": "Cheerful & Upbeat",
    "dramatic": "Dramatic & Emotional",
    "news": "News Broadcaster",
    "storytelling": "Storytelling Narrator",
}

STYLE_PROMPTS = {
    "neutral": "Natural speech style",
    "cheerful": "Upbeat and enthusiastic tone",
    "dramatic": "Expressive and dramatic tone",
    "news": "Clear and authoritative news style",
    "storytelling": "Engaging storytelling tone",
}

VOICE_MODES = {
    "single": "Single Speaker (uses current selected voice)",
    "multi": "Multi-Speaker (auto-selects voices per character)",
}

current_voice = DEFAULT_VOICE if DEFAULT_VOICE in VOICES else "Kore"
current_speed = DEFAULT_SPEED if DEFAULT_SPEED in SPEEDS else "normal"
current_style = DEFAULT_STYLE if DEFAULT_STYLE in STYLES else "neutral"
current_voice_mode = DEFAULT_VOICE_MODE if DEFAULT_VOICE_MODE in VOICE_MODES else "single"


def is_owner(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return not ALLOWED_TELEGRAM_USER_IDS or user.id in ALLOWED_TELEGRAM_USER_IDS


async def reject_unauthorized(update: Update) -> None:
    if update.message:
        await update.message.reply_text("Unauthorized user. Access denied.")


def progress_bar(percent: int) -> str:
    filled = min(10, max(0, percent // 10))
    return f"[{'█' * filled}{'░' * (10 - filled)}] {percent}%"


async def animate_progress(message, base_text: str):
    steps = [15, 35, 60, 80, 95]
    try:
        for percent in steps:
            await asyncio.sleep(1.2)
            await message.edit_text(f"{base_text}... {progress_bar(percent)}")
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def welcome_text() -> str:
    return (
        "<b>Gemini Text-to-Speech Bot</b>\n\n"
        f"Current Voice: {current_voice} ({VOICES[current_voice]['gender']}, {VOICES[current_voice]['tone']})\n"
        f"Voice mode: {VOICE_MODES[current_voice_mode]}\n"
        f"Current speed: {SPEED_LABELS[current_speed]}\n\n"
        f"Current style: {STYLE_NAMES[current_style]}\n\n"
        "Choose a setting below, then send me any text.\n\n"
        "Optional voice cloning: /clonevoice, then send a short audio sample. Use /cancelclone to return to Gemini TTS."
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🎙 Set Voice", callback_data="menu:voice"),
            InlineKeyboardButton("⚡ Set Speed", callback_data="menu:speed"),
        ],
        [
            InlineKeyboardButton("🎭 Set Style", callback_data="menu:style"),
            InlineKeyboardButton("👥 Voice Mode", callback_data="menu:mode"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


class GeminiTTSAPIError(RuntimeError):
    def __init__(self, status_code: int, model: str, body: str):
        self.status_code = status_code
        self.model = model
        self.body = body
        super().__init__(f"Gemini TTS API error {status_code} for {model}: {body[:1000]}")


class GeminiTTSRequestError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Gemini TTS request {reason}")


class VoiceCloneAPIError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Voice-cloning provider returned HTTP {status_code}")


class VoiceCloneConfigurationError(RuntimeError):
    pass


def _generate_voice_clone_audio(
    text: str, reference_audio: bytes, reference_transcript: str, speed: str
) -> bytes:
    if not VOICE_CLONE_API_KEY:
        raise VoiceCloneConfigurationError("Voice-cloning API key is not configured")
    payload = {
        "text": text,
        "references": [{"audio": reference_audio, "text": reference_transcript}],
        "format": "mp3",
        "normalize": True,
        "prosody": {"speed": VOICE_CLONE_SPEEDS.get(speed, 1.0)},
    }
    response = requests.post(
        f"{VOICE_CLONE_API_BASE_URL}/v1/tts",
        headers={
            "Authorization": f"Bearer {VOICE_CLONE_API_KEY}",
            "Content-Type": "application/msgpack",
            "model": VOICE_CLONE_TTS_MODEL,
        },
        data=msgpack.packb(payload, use_bin_type=True),
        timeout=(15, 120),
    )
    if not response.ok:
        raise VoiceCloneAPIError(response.status_code)
    if not response.content:
        raise RuntimeError("Voice-cloning provider returned empty audio")
    if "json" in response.headers.get("Content-Type", "").lower():
        raise RuntimeError("Voice-cloning provider returned an error instead of audio")
    return response.content


def compile_cloned_voice_to_mp3(
    text: str, reference_audio_path: Path, reference_transcript: str, speed: str
) -> Path:
    chunks = split_text_for_tts(text)
    if not chunks or not any(chunk.strip() for chunk in chunks):
        raise ValueError("Please send non-empty text to synthesize")
    reference_audio = reference_audio_path.read_bytes()
    if not reference_audio or not reference_transcript.strip():
        raise ValueError("The reference audio and its exact transcript are both required")
    master_dir = Path(tempfile.mkdtemp(prefix="telegram-voice-clone-"))
    segment_paths: list[Path] = []
    try:
        for index, chunk in enumerate(chunks, start=1):
            if index > 1 and TTS_INTER_SEGMENT_DELAY_SECONDS > 0:
                time.sleep(TTS_INTER_SEGMENT_DELAY_SECONDS)
            audio_bytes = _generate_voice_clone_audio(
                chunk, reference_audio, reference_transcript, speed
            )
            segment_path = master_dir / f"segment_{index:04d}.mp3"
            segment_path.write_bytes(audio_bytes)
            segment_paths.append(segment_path)
        if len(segment_paths) == 1:
            return segment_paths[0]
        concat_list = master_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{path.name}'" for path in segment_paths) + "\n", encoding="utf-8"
        )
        final_path = master_dir / "voice-clone.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c:a", "libmp3lame", "-b:a", "192k", "-vn", str(final_path)],
            check=True,
            timeout=max(120, 30 * len(segment_paths)),
            cwd=master_dir,
        )
        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty cloned-voice MP3")
        return final_path
    except Exception:
        shutil.rmtree(master_dir, ignore_errors=True)
        raise


def transcode_clone_sample(source_path: Path, target_path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source_path),
         "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(target_path)],
        check=True,
        timeout=30,
    )
    if not target_path.exists() or target_path.stat().st_size == 0:
        raise RuntimeError("Audio conversion produced an empty WAV sample")


def probe_audio_duration(audio_path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def validate_voice_clone_sample_duration(duration: float | int | None) -> None:
    if duration is None:
        raise ValueError(
            "Could not verify the audio duration. Please send a supported clip between "
            f"{VOICE_CLONE_MIN_AUDIO_SECONDS} and {VOICE_CLONE_MAX_AUDIO_SECONDS} seconds long."
        )
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        seconds = float("nan")
    if not math.isfinite(seconds):
        raise ValueError(
            "Could not verify the audio duration. Please send a supported clip between "
            f"{VOICE_CLONE_MIN_AUDIO_SECONDS} and {VOICE_CLONE_MAX_AUDIO_SECONDS} seconds long."
        )
    if not VOICE_CLONE_MIN_AUDIO_SECONDS <= seconds <= VOICE_CLONE_MAX_AUDIO_SECONDS:
        raise ValueError(
            f"Please send a clip between {VOICE_CLONE_MIN_AUDIO_SECONDS} and "
            f"{VOICE_CLONE_MAX_AUDIO_SECONDS} seconds long."
        )


def clear_voice_clone_session(user_data: dict) -> None:
    sample_path = user_data.pop("voice_clone_sample_path", None)
    for key in (
        "voice_clone_pending_audio",
        "voice_clone_pending_transcript",
        "voice_clone_transcript",
        "voice_clone_model_id",
    ):
        user_data.pop(key, None)
    if sample_path:
        shutil.rmtree(Path(sample_path).parent, ignore_errors=True)


def clone_audio_attachment(message):
    if message.voice:
        return message.voice, "telegram-voice.opus", message.voice.duration, None
    attachment = message.audio or message.document
    if not attachment:
        return None, None, None, "Please send a Telegram voice note or an MP3, WAV, M4A, or Opus audio file."
    filename = getattr(attachment, "file_name", None) or ""
    mime_type = (getattr(attachment, "mime_type", None) or "").lower()
    suffix = Path(filename).suffix.lower()
    if not suffix:
        suffix = {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/mp4": ".m4a",
            "audio/ogg": ".opus",
            "audio/opus": ".opus",
        }.get(mime_type, "")
    if suffix not in VOICE_CLONE_AUDIO_SUFFIXES:
        return None, None, None, "Unsupported audio format. Send a voice note, MP3, WAV, M4A, or Opus file."
    safe_suffix = ".opus" if suffix in {".ogg", ".oga"} else suffix
    duration = getattr(attachment, "duration", None)
    return attachment, f"voice-sample{safe_suffix}", duration, None


def _build_gemini_tts_payload(
    text: str, voice: str, speed: str, style: str, model: str
) -> dict:
    delivery = f"{STYLE_PROMPTS[style]}; {SPEEDS[speed]}"
    return {
        "model": model,
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {"type": "speech_metadata", "style": delivery}
                        ],
                    }
                ],
            }
        ],
        "response_format": {
            "type": "audio",
            "mime_type": "audio/l16",
            "sample_rate": 24000,
        },
        "generation_config": {"speech_config": [{"voice": voice}]},
    }


def _extract_interaction_audio_base64(data: dict) -> str | None:
    output_audio = data.get("output_audio")
    if isinstance(output_audio, dict) and isinstance(output_audio.get("data"), str):
        return output_audio["data"]

    steps = data.get("steps", [])
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            continue
        content = step.get("content", [])
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("data"), str):
                return block["data"]
    return None


def _generate_audio_base64(text: str, voice: str, model: str, speed: str, style: str) -> str:
    payload = _build_gemini_tts_payload(text, voice, speed, style, model)
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    try:
        res = requests.post(
            GEMINI_INTERACTIONS_URL, headers=headers, json=payload, timeout=60
        )
    except requests.exceptions.Timeout as exc:
        raise GeminiTTSRequestError("timed out") from exc
    except requests.exceptions.ConnectionError as exc:
        raise GeminiTTSRequestError("could not connect") from exc
    except requests.exceptions.RequestException as exc:
        raise GeminiTTSRequestError("failed due to a network error") from exc
    if not res.ok:
        raise GeminiTTSAPIError(res.status_code, model, res.text)

    try:
        data = res.json()
    except ValueError as exc:
        raise RuntimeError(f"Gemini returned an invalid JSON response ({model}).") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected API response structure from Gemini ({model}).")
    audio_data = _extract_interaction_audio_base64(data)
    if audio_data:
        return audio_data
    raise RuntimeError(f"No audio data returned in Gemini Interactions response ({model}).")


def generate_audio_segment(text: str, voice: str, speed: str, style: str) -> bytes:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    models_to_try = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        models_to_try.append(GEMINI_FALLBACK_MODEL)

    last_exception = None
    for attempt_model in models_to_try:
        for attempt in range(1, TTS_FALLBACK_RETRIES + 1):
            try:
                b64_str = _generate_audio_base64(text, voice, attempt_model, speed, style)
                return base64.b64decode(b64_str)
            except GeminiTTSAPIError as exc:
                last_exception = exc
                if exc.status_code == 429:
                    logger.warning("Quota 429 on %s (attempt %d/%d).", attempt_model, attempt, TTS_FALLBACK_RETRIES)
                    if attempt < TTS_FALLBACK_RETRIES:
                        time.sleep(TTS_RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    break
                else:
                    logger.error("API Error %d on %s: %s", exc.status_code, attempt_model, exc.body[:200])
                    break
            except Exception as exc:
                last_exception = exc
                logger.error("Unexpected error generating audio with %s: %s", attempt_model, str(exc))
                break

    if last_exception:
        raise last_exception
    raise RuntimeError("Failed to generate audio segment.")


def split_text_for_tts(text: str, max_chunk_len: int = 800) -> list[str]:
    lines = text.split("\n")
    chunks = []
    current_chunk = ""

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        if len(current_chunk) + len(line_str) + 1 <= max_chunk_len:
            current_chunk = f"{current_chunk}\n{line_str}" if current_chunk else line_str
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(line_str) > max_chunk_len:
                sentences = re.split(r"(?<=[.!?])\s+", line_str)
                temp_chunk = ""
                for s in sentences:
                    if len(temp_chunk) + len(s) + 1 <= max_chunk_len:
                        temp_chunk = f"{temp_chunk} {s}" if temp_chunk else s
                    else:
                        if temp_chunk:
                            chunks.append(temp_chunk)
                        temp_chunk = s
                if temp_chunk:
                    current_chunk = temp_chunk
                else:
                    current_chunk = ""
            else:
                current_chunk = line_str

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def parse_script(text: str) -> list[tuple[str, str]]:
    lines = text.split("\n")
    dialogue = []
    character_voices = {}
    voice_pool = list(VOICES.keys())
    voice_index = 0

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        match = re.match(r"^([A-Za-z0-9_\s]{2,20}):\s*(.+)$", line_str)
        if match:
            char_name = match.group(1).strip()
            speech = match.group(2).strip()
            if char_name not in character_voices:
                assigned_voice = voice_pool[voice_index % len(voice_pool)]
                character_voices[char_name] = assigned_voice
                voice_index += 1
            dialogue.append((char_name, speech, character_voices[char_name]))
        else:
            dialogue.append(("Narrator", line_str, current_voice))

    return dialogue


def compile_script_to_mp3(
    text: str, voice_mode: str, speed: str, style: str
) -> Path:
    if voice_mode == "single":
        chunks = split_text_for_tts(text)
        segments = [("Narrator", chunk, current_voice) for chunk in chunks]
    else:
        segments = parse_script(text)

    if not segments:
        raise ValueError("Text contains no speakable lines.")

    master_dir = Path(tempfile.mkdtemp(prefix="telegram-tts-"))
    segment_paths = []

    try:
        for idx, (speaker, speech_text, voice_name) in enumerate(segments, start=1):
            if idx > 1 and TTS_INTER_SEGMENT_DELAY_SECONDS > 0:
                time.sleep(TTS_INTER_SEGMENT_DELAY_SECONDS)

            pcm_bytes = generate_audio_segment(speech_text, voice_name, speed, style)
            raw_path = master_dir / f"segment_{idx:04d}.pcm"
            mp3_path = master_dir / f"segment_{idx:04d}.mp3"
            raw_path.write_bytes(pcm_bytes)

            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "s16le", "-ar", "24000", "-ac", "1",
                    "-i", str(raw_path),
                    "-c:a", "libmp3lame", "-b:a", "192k",
                    str(mp3_path)
                ],
                check=True,
                timeout=30,
            )
            segment_paths.append(mp3_path)

        if len(segment_paths) == 1:
            return segment_paths[0]

        concat_list = master_dir / "concat.txt"
        with concat_list.open("w", encoding="utf-8") as f:
            for p in segment_paths:
                f.write(f"file '{p.name}'\n")

        final_mp3 = master_dir / "full_audio.mp3"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(final_mp3)
            ],
            check=True,
            timeout=60,
        )
        return final_mp3

    except Exception:
        shutil.rmtree(master_dir, ignore_errors=True)
        raise


def convert_to_voice_note(mp3_path: Path) -> Path:
    ogg_path = mp3_path.with_suffix(".ogg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(mp3_path),
            "-c:a", "libopus", "-b:a", "32k",
            str(ogg_path)
        ],
        check=True,
        timeout=30,
    )
    return ogg_path


def tts_failure_message(exc: Exception) -> str:
    if isinstance(exc, VoiceCloneConfigurationError):
        return "Voice cloning is not configured. Add VOICE_CLONE_API_KEY (or FISH_API_KEY) in Render Environment; do not send API keys in Telegram."
    if isinstance(exc, GeminiTTSRequestError):
        return f"Gemini TTS request {exc.reason}. Please try again in a few minutes."
    if isinstance(exc, VoiceCloneAPIError):
        if exc.status_code in {401, 403}:
            return "The voice-cloning API key is missing, invalid, or lacks access. Check VOICE_CLONE_API_KEY in Render Environment."
        if exc.status_code == 402:
            return "The voice-cloning provider reports no available free-model credit. Confirm VOICE_CLONE_TTS_MODEL is s2.1-pro-free, then check the provider account or wait for its free allowance to return."
        if exc.status_code == 429:
            return "The voice-cloning provider's free-tier quota or rate limit has been reached. Please wait and try again later."
        if exc.status_code in {500, 502, 503, 504}:
            return "The voice-cloning provider is temporarily busy. Please try again in a few minutes."
        if exc.status_code == 413:
            return "The voice sample is too large for the provider. Send a shorter or smaller audio clip."
        if exc.status_code == 400:
            return "The provider rejected this sample or request. Use a clear, single-speaker clip of at least 10 seconds in MP3, WAV, M4A, or Opus format."
        return "The voice-cloning API request failed. Please check the provider settings and try again."
    if isinstance(exc, requests.exceptions.Timeout):
        return "The voice-cloning provider timed out. Try shorter text or retry in a few minutes."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Could not connect to the voice-cloning provider. Please try again in a few minutes."
    if isinstance(exc, requests.exceptions.RequestException):
        return "The voice-cloning provider request failed. Please try again in a few minutes."
    if isinstance(exc, GeminiTTSAPIError):
        if exc.status_code == 429:
            return (
                "Daily or minute Gemini API rate limits hit (HTTP 429).\n\n"
                "Suggestions:\n"
                "• Wait a minute and try again.\n"
                "• Check your Google AI Studio quota limits."
            )
        if exc.status_code == 400:
            return (
                "Gemini rejected the text-to-speech request (HTTP 400). "
                "Please retry; if it repeats, contact the bot owner."
            )
        return f"Gemini API returned error code {exc.status_code}."
    return f"Failed to generate speech: {str(exc)}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    if update.message:
        await update.message.reply_text(
            welcome_text(),
            reply_markup=settings_keyboard(),
            parse_mode="HTML"
        )


async def setvoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    keyboard = []
    for v_name, meta in VOICES.items():
        label = f"{v_name} ({meta['gender']}, {meta['tone']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"voice:{v_name}")])
    keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data="menu:main")])
    if update.message:
        await update.message.reply_text("Select a voice:", reply_markup=InlineKeyboardMarkup(keyboard))


async def setspeed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    keyboard = []
    for spd_key, spd_label in SPEED_LABELS.items():
        keyboard.append([InlineKeyboardButton(spd_label, callback_data=f"speed:{spd_key}")])
    keyboard.append([InlineKeyboardButton("« Back to Settings", callback_data="menu:main")])
    if update.message:
        await update.message.reply_text("Select speed:", reply_markup=InlineKeyboardMarkup(keyboard))


async def clonevoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    if not update.message:
        return
    if not VOICE_CLONE_API_KEY:
        await update.message.reply_text(
            "Voice cloning is not configured yet. Add VOICE_CLONE_API_KEY (or FISH_API_KEY) in Render Environment, then restart the service. Do not send API keys in chat."
        )
        return
    context.user_data["voice_clone_pending_audio"] = True
    await update.message.reply_text(
        "Send a clear, single-speaker voice note or MP3/WAV/M4A/Opus clip, "
        f"{VOICE_CLONE_MIN_AUDIO_SECONDS}–{VOICE_CLONE_MAX_AUDIO_SECONDS} seconds and up to "
        f"{VOICE_CLONE_MAX_UPLOAD_MB} MB. Only send your own voice or a voice you have explicit permission to clone. "
        "Fish Audio's free instant-clone request needs the exact words spoken in the sample; after uploading, send that transcript. "
        "The sample is sent with each generation request and is not saved as a provider-side voice model. Use /cancelclone to stop."
    )


async def cancelclone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    clear_voice_clone_session(context.user_data)
    if update.message:
        await update.message.reply_text(
            "Cloned-voice mode is off. The temporary sample was removed from this bot instance. New text will use the existing Gemini TTS behavior."
        )


async def clone_audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    message = update.message
    if not message:
        return
    if not context.user_data.get("voice_clone_pending_audio"):
        await handle_other_message(update, context)
        return
    if not VOICE_CLONE_API_KEY:
        context.user_data.pop("voice_clone_pending_audio", None)
        await message.reply_text(tts_failure_message(VoiceCloneConfigurationError("missing key")))
        return
    attachment, upload_name, duration, error = clone_audio_attachment(message)
    if error:
        await message.reply_text(error)
        return
    max_bytes = VOICE_CLONE_MAX_UPLOAD_MB * 1024 * 1024
    if attachment.file_size and attachment.file_size > max_bytes:
        await message.reply_text(f"That file is too large. The current limit is {VOICE_CLONE_MAX_UPLOAD_MB} MB.")
        return
    if duration is not None:
        try:
            validate_voice_clone_sample_duration(duration)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return

    temp_dir = Path(tempfile.mkdtemp(prefix="telegram-clone-upload-"))
    source_path = temp_dir / upload_name
    sample_path = temp_dir / "reference.wav"
    keep_sample = False
    status_message = await message.reply_text("Receiving the sample and preparing it for instant voice cloning…")
    try:
        telegram_file = await context.bot.get_file(attachment.file_id)
        await telegram_file.download_to_drive(custom_path=str(source_path))
        if not source_path.exists() or source_path.stat().st_size == 0:
            raise RuntimeError("Telegram returned an empty audio file")
        if source_path.stat().st_size > max_bytes:
            raise ValueError(f"The downloaded file exceeds the {VOICE_CLONE_MAX_UPLOAD_MB} MB limit")
        measured_duration = await asyncio.to_thread(probe_audio_duration, source_path)
        validate_voice_clone_sample_duration(measured_duration)
        await asyncio.to_thread(transcode_clone_sample, source_path, sample_path)
        source_path.unlink(missing_ok=True)
        clear_voice_clone_session(context.user_data)
        context.user_data["voice_clone_sample_path"] = str(sample_path)
        context.user_data["voice_clone_pending_transcript"] = True
        context.user_data.pop("voice_clone_pending_audio", None)
        keep_sample = True
        await status_message.edit_text(
            "Audio sample is ready. Now send the exact words spoken in that sample. After that, send the text to synthesize. Use /cancelclone to remove the local sample."
        )
    except Exception as exc:
        logger.warning("Voice clone setup failed: %s", type(exc).__name__)
        detail = str(exc) if isinstance(exc, ValueError) else tts_failure_message(exc)
        await status_message.edit_text(f"Voice-clone setup failed. {detail}")
    finally:
        if not keep_sample:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_voice, current_speed, current_style, current_voice_mode
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_owner(update):
        await query.edit_message_text("Unauthorized user.")
        return

    data = query.data
    if data == "menu:main":
        await query.edit_message_text(
            welcome_text(),
            reply_markup=settings_keyboard(),
            parse_mode="HTML"
        )
    elif data == "menu:voice":
        keyboard = []
        for v_name, meta in VOICES.items():
            prefix = "✓ " if v_name == current_voice else ""
            label = f"{prefix}{v_name} ({meta['gender']}, {meta['tone']})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"voice:{v_name}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="menu:main")])
        await query.edit_message_text("Select a voice:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("voice:"):
        v_name = data.split(":")[1]
        if v_name in VOICES:
            current_voice = v_name
            await query.edit_message_text(
                f"Voice changed to <b>{current_voice}</b>.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="menu:main")]]),
                parse_mode="HTML"
            )
    elif data == "menu:speed":
        keyboard = []
        for spd_key, spd_label in SPEED_LABELS.items():
            prefix = "✓ " if spd_key == current_speed else ""
            keyboard.append([InlineKeyboardButton(f"{prefix}{spd_label}", callback_data=f"speed:{spd_key}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="menu:main")])
        await query.edit_message_text("Select speed:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("speed:"):
        spd_key = data.split(":")[1]
        if spd_key in SPEEDS:
            current_speed = spd_key
            await query.edit_message_text(
                f"Speed changed to <b>{SPEED_LABELS[current_speed]}</b>.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="menu:main")]]),
                parse_mode="HTML"
            )
    elif data == "menu:style":
        keyboard = []
        for st_key, st_name in STYLE_NAMES.items():
            prefix = "✓ " if st_key == current_style else ""
            keyboard.append([InlineKeyboardButton(f"{prefix}{st_name}", callback_data=f"style:{st_key}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="menu:main")])
        await query.edit_message_text("Select voice style:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("style:"):
        st_key = data.split(":")[1]
        if st_key in STYLES:
            current_style = st_key
            await query.edit_message_text(
                f"Style changed to <b>{STYLE_NAMES[current_style]}</b>.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="menu:main")]]),
                parse_mode="HTML"
            )
    elif data == "menu:mode":
        keyboard = [
            [InlineKeyboardButton("Single Speaker", callback_data="mode:single")],
            [InlineKeyboardButton("Multi-Speaker (Script)", callback_data="mode:multi")],
            [InlineKeyboardButton("« Back", callback_data="menu:main")],
        ]
        await query.edit_message_text("Select voice mode:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("mode:"):
        mode_key = data.split(":")[1]
        if mode_key in VOICE_MODES:
            current_voice_mode = mode_key
            await query.edit_message_text(
                f"Voice mode changed to <b>{VOICE_MODES[current_voice_mode]}</b>.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="menu:main")]]),
                parse_mode="HTML"
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
    if context.user_data.get("voice_clone_pending_audio"):
        await update.message.reply_text("Please send the audio sample first, or use /cancelclone to stop setup.")
        return
    if context.user_data.get("voice_clone_pending_transcript"):
        if len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(f"Please keep the reference transcript under {MAX_TEXT_LENGTH} characters.")
            return
        context.user_data["voice_clone_transcript"] = text
        context.user_data.pop("voice_clone_pending_transcript", None)
        await update.message.reply_text("Reference transcript saved. Now send the text you want spoken in that voice.")
        return
    clone_sample_path = context.user_data.get("voice_clone_sample_path")
    clone_transcript = context.user_data.get("voice_clone_transcript")
    if clone_sample_path and clone_transcript:
        if len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(
                f"Text is too long ({len(text)} characters). Maximum limit is {MAX_TEXT_LENGTH}."
            )
            return
        sample_path = Path(clone_sample_path)
        if not sample_path.exists():
            clear_voice_clone_session(context.user_data)
            await update.message.reply_text("The temporary voice sample has expired. Start again with /clonevoice.")
            return
        status_message = await update.message.reply_text(
            f"Generating speech with your private cloned voice... {progress_bar(5)}"
        )
        progress_task = None
        audio_path = None
        voice_note_path = None
        voice_note_sent = False
        try:
            spoken_text = " ".join(
                segment_text for _, segment_text, _ in parse_script(text)
            ).strip()
            if not spoken_text:
                raise ValueError("Please send non-empty text to synthesize.")
            progress_task = asyncio.create_task(
                animate_progress(status_message, "Generating cloned-voice audio")
            )
            await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            audio_path = await asyncio.to_thread(
                compile_cloned_voice_to_mp3, spoken_text, sample_path, clone_transcript, current_speed
            )
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
            try:
                voice_note_path = await asyncio.to_thread(convert_to_voice_note, audio_path)
            except Exception:
                logger.exception("Could not convert cloned MP3 to a Telegram voice note; sending the MP3 only.")
            if voice_note_path:
                try:
                    with voice_note_path.open("rb") as voice_note_file:
                        await update.message.reply_voice(
                            voice=voice_note_file,
                            caption="Speech generated with your cloned voice",
                        )
                    voice_note_sent = True
                except Exception:
                    logger.exception("Could not deliver cloned voice note; sending the MP3 document instead.")
            with audio_path.open("rb") as audio_file:
                await update.message.reply_document(
                    document=audio_file,
                    filename="cloned-voiceover.mp3",
                    caption="Speech generated with your cloned voice",
                )
            if voice_note_sent:
                await status_message.edit_text(f"Cloned voiceover ready... {progress_bar(100)}")
            else:
                await status_message.edit_text(
                    f"Cloned MP3 sent. Telegram voice-note format was unavailable... {progress_bar(100)}"
                )
        except Exception as exc:
            if progress_task:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if isinstance(exc, VoiceCloneAPIError):
                logger.exception("Cloned-voice synthesis failed with HTTP %s.", exc.status_code)
            else:
                logger.exception("Cloned-voice synthesis failed (%s).", type(exc).__name__)
            await status_message.edit_text(
                f"Cloned-voice generation failed... {progress_bar(100)}\n{tts_failure_message(exc)}"
            )
        finally:
            if audio_path:
                shutil.rmtree(audio_path.parent, ignore_errors=True)
        return
    if current_voice_mode == "single":
        segments = [("Narrator", chunk, current_voice) for chunk in split_text_for_tts(text)]
        mode_label = VOICE_MODES["single"]
    else:
        segments = parse_script(text)
        mode_label = VOICE_MODES["multi"]

    if len(text) > MAX_TEXT_LENGTH:
        await update.message.reply_text(
            f"Text is too long ({len(text)} characters). Maximum limit is {MAX_TEXT_LENGTH}."
        )
        return

    status_message = await update.message.reply_text(
        f"Generating TTS audio ({mode_label})... {progress_bar(5)}"
    )
    progress_task = asyncio.create_task(animate_progress(status_message, "Generating TTS audio"))

    mp3_file_path = None
    voice_note_path = None
    try:
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        mp3_file_path = await asyncio.to_thread(
            compile_script_to_mp3, text, current_voice_mode, current_speed, current_style
        )
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)

        voice_note_path = await asyncio.to_thread(convert_to_voice_note, mp3_file_path)

        with voice_note_path.open("rb") as vn_file:
            await update.message.reply_voice(
                voice=vn_file,
                caption=f"Voice: {current_voice} | Speed: {SPEED_LABELS[current_speed]} | Mode: {current_voice_mode}"
            )

        with mp3_file_path.open("rb") as mp3_file:
            await update.message.reply_document(
                document=mp3_file,
                filename="generated_speech.mp3",
                caption="High-quality MP3 audio file"
            )

        await status_message.edit_text(f"Speech generated successfully! {progress_bar(100)}")

    except Exception as exc:
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)
        logger.error("Error handling TTS text: %s", str(exc))
        await status_message.edit_text(
            f"Generation failed... {progress_bar(100)}\n{tts_failure_message(exc)}"
        )
    finally:
        if mp3_file_path and mp3_file_path.exists():
            shutil.rmtree(mp3_file_path.parent, ignore_errors=True)


async def handle_other_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_unauthorized(update)
        return
    if update.message:
        await update.message.reply_text("Please send plain text to generate speech.")


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


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setvoice", setvoice_command))
    application.add_handler(CommandHandler("setspeed", setspeed_command))
    application.add_handler(CommandHandler("clonevoice", clonevoice_command))
    application.add_handler(CommandHandler("cancelclone", cancelclone_command))
    application.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^(menu|voice|speed|style|mode|confirm|back):"))
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.ALL, clone_audio_handler)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_other_message))
    return application


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
        exit(1)
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY environment variable is not set.")
        exit(1)

    start_health_server()
    app = build_application()
    logger.info("Bot is starting polling...")
    app.run_polling()
