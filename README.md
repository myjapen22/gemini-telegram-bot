# Telegram Gemini Text-to-Speech Bot

Private Telegram bot using Google Gemini TTS. It generates Burmese or multilingual audio, converts Gemini WAV output to downloadable MP3 files with FFmpeg, and sends them through Telegram's standard audio upload.

## Render free web-service mode

This project runs Telegram long polling and a small HTTP health server in the same process. Render uses `/health` to check that the process is alive. The web service binds to `0.0.0.0:$PORT`.

The included `render.yaml` creates a Docker-based free web service. An external monitor such as UptimeRobot can request the Render `/health` URL periodically, but free-host sleep policies and provider limits can change; this is a best-effort workaround, not a guaranteed 24/7 SLA.

## Included deployment files

- `bot.py` — Telegram bot, Gemini TTS, MP3 delivery, long-text chunking, multi-user authorization, and `/health`.
- `requirements.txt` — Python dependencies.
- `Dockerfile` — Python + FFmpeg runtime.
- `render.yaml` — Render free web-service Blueprint.
- `.env.example` — safe environment template.

## Required environment variables

```text
TELEGRAM_BOT_TOKEN=<BotFather token>
GEMINI_API_KEY=<Google AI Studio API key>
ALLOWED_TELEGRAM_USER_IDS=7589831382
GEMINI_MODEL=gemini-3.8-flash-tts
GEMINI_FALLBACK_MODEL=gemini-3.8-flash-lite-tts
DEFAULT_VOICE=Kore
DEFAULT_SPEED=normal
MAX_TEXT_LENGTH=4000
```

## Render deployment

1. Create a private GitHub repository and push this project.
2. In Render, choose **New → Blueprint**, select the repository, and apply `render.yaml`.
3. Enter `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` when Render asks for secret values.
4. Confirm the service is a **Web Service** on the `free` plan.
5. After deployment, open the service URL plus `/health`. It should return:

   ```json
   {"status":"ok","service":"telegram-gemini-tts"}
   ```

6. For best-effort wakeups, create an UptimeRobot HTTP monitor for `https://<your-render-subdomain>.onrender.com/health` with a 5-minute interval. UptimeRobot requires its own account and may not prevent all platform sleep behavior.

Use exactly one running instance for a given Telegram bot token.

## Local Docker test

```bash
docker build -t telegram-gemini-tts .
docker run --rm -p 10000:10000 --env-file .env telegram-gemini-tts
curl http://localhost:10000/health
```

## Local Python test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg
cp .env.example .env
python test_config.py
python bot.py
```

## Bot commands

- `/start`
- `/setvoice` — complete Gemini prebuilt voice catalog
- `/setspeed` — Slow, Normal, Fast
- `/clonevoice` — optional Fish Audio voice cloning; use a clear 15–30 second reference sample and an exact transcript. Add `FISH_AUDIO_API_KEY` in Render Environment (`VOICE_CLONE_API_KEY` and `FISH_API_KEY` remain supported aliases). Once setup is complete, send text for clone TTS or a Burmese voice note for transcription followed by synthesis in the saved reference voice. Voice-note output depends on transcription quality and is not direct acoustic voice conversion or a guarantee of perfect pronunciation.
- `/cancelclone` — remove the temporary clone sample and return to Gemini TTS.

For Burmese clone speech, the bot normalizes Unicode to NFC, tidies whitespace, converts Myanmar digits to standard Arabic digits in synthesis text, and preserves Burmese combining marks and joiners. The reference transcript keeps its original spoken words and digit forms, with spacing cleanup only, so it stays aligned with the uploaded sample. Fish Audio's text rewriter is disabled for clone requests because its documented number-normalization language list does not include Burmese. Synthesis uses the documented `s2.1-pro-free` model and MP3 output at 44.1 kHz / 192 kbps, with the provider's documented sampling defaults.

Pronunciation changes are optional and disabled by default. To replace exact literal text only in synthesized narration (never in the reference transcript), set `VOICE_CLONE_PRONUNCIATION_OVERRIDES` to a JSON object in Render Environment, for example `{"ကျွန်တော်":"ကျနော်"}`. Use only replacements that preserve the intended meaning and test the result with a short sentence.

Messages longer than 4,000 characters are split safely into multiple downloadable MP3 files. Unauthorized users receive a private-bot message and cannot call Gemini.

The primary model is Google's Gemini 3.8 Flash TTS model. If it returns a temporary 429/5xx error, the bot makes one attempt with the supported Gemini 3.8 Flash-Lite TTS model. A 429 caused by the project's Free Tier daily quota cannot be bypassed by changing the API key or using `gemini-2.0-flash`; that model is not a TTS audio-output model. The bot logs the exact status, model, and sanitized API response and reports quota failures clearly in Telegram.
