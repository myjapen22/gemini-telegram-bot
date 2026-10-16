import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

required = ["TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "ALLOWED_TELEGRAM_USER_IDS"]
missing = [name for name in required if not os.getenv(name)]
if missing:
    raise SystemExit("Missing environment variables: " + ", ".join(missing))

configured_ids = [value.strip() for value in os.environ["ALLOWED_TELEGRAM_USER_IDS"].split(",")]
if not configured_ids or any(not value.isdigit() or int(value) <= 0 for value in configured_ids):
    raise SystemExit("ALLOWED_TELEGRAM_USER_IDS must contain positive numeric IDs separated by commas")

import bot  # noqa: E402

assert len(bot.VOICE_CATALOG) == 30
assert {"Aoede", "Charon", "Fenrir", "Kore", "Puck", "Despina", "Iapetus", "Rasalgethi", "Sadachbia", "Sulafat", "Zephyr"} <= bot.VOICE_NAMES
assert set(bot.SPEEDS) == {"slow", "normal", "fast"}
assert bot.ALLOWED_TELEGRAM_USER_IDS
app = bot.build_application()
assert app is not None
print("Gemini TTS configuration and application construction passed; no network calls were made.")
