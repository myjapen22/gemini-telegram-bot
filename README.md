# Telegram Gemini TTS Bot

Private, owner-restricted Telegram bot using Google Gemini TTS. It generates Burmese or multilingual audio, converts Gemini WAV output to downloadable MP3 files with FFmpeg, and sends them through Telegram's standard audio upload.

## Included deployment files

- `bot.py` — complete bot with long-text chunking, 30 Gemini voices, `/setvoice`, `/setspeed`, fixed cinematic storytelling style, MP3 output, and multi-user authorization.
- `requirements.txt` — Python dependencies.
- `Dockerfile` — Python + FFmpeg runtime for Render or any Docker host.
- `render.yaml` — Render Background Worker Blueprint.
- `.env.example` — safe environment template with no secrets.

## GitHub safety

Never commit `.env`, API keys, or bot tokens. The repository should contain `.env.example`, not `.env`. Rotate any token or API key that has been exposed.

## Required environment variables

```text
TELEGRAM_BOT_TOKEN=<BotFather token>
GEMINI_API_KEY=<Google AI Studio API key>
ALLOWED_TELEGRAM_USER_IDS=7589831382
GEMINI_MODEL=gemini-3.8-flash-tts
DEFAULT_VOICE=Kore
DEFAULT_SPEED=normal
MAX_TEXT_LENGTH=4000
```

`ALLOWED_TELEGRAM_USER_IDS` accepts comma-separated numeric IDs, for example `7589831382,123456789`.

## Render deployment

1. Create a **private GitHub repository** and upload the files in this project. Do not upload `.env` or `.venv`.
2. In Render, choose **New → Blueprint** and select the repository, or create a Background Worker using the Dockerfile.
3. The `render.yaml` file creates a Docker worker with `python bot.py` as the container command.
4. Enter `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` as secret environment values when Render prompts for them.
5. Deploy and inspect logs for `Application started` and repeated Telegram `getUpdates` calls.

Use a worker/background service, not a web service. Keep exactly one running worker for a given Telegram bot token.

## Local Docker test

```bash
docker build -t telegram-gemini-tts .
docker run --rm --env-file .env telegram-gemini-tts
```

## Local Python test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg
cp .env.example .env
# Fill in .env
python test_config.py
python bot.py
```

## Bot commands

- `/start`
- `/setvoice` — complete Gemini prebuilt voice catalog
- `/setspeed` — Slow, Normal, Fast

Messages longer than 4,000 characters are split safely into multiple downloadable MP3 files. Unauthorized users receive a private-bot message and cannot call Gemini.

## Free hosting caveat

Render's free-tier availability, worker support, sleeping behavior, and limits can change. A true always-on 24/7 worker may require a paid plan or another host even though this project itself has no hosting-specific license cost.
