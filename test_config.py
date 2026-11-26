import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

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

assert len(bot.VOICES) == 30
assert {"Achernar", "Achird", "Kore", "Puck", "Zubenelgenubi"} <= set(bot.VOICES)
assert set(bot.VOICE_MODES) == {"single", "multi"}
assert set(bot.SPEEDS) == set(bot.SPEED_LABELS)
assert bot.ALLOWED_TELEGRAM_USER_IDS
app = bot.build_application()
assert app is not None

sample_root = Path(tempfile.mkdtemp(prefix="voice-clone-config-test-"))
sample_dir = sample_root / "session"
sample_dir.mkdir()
sample_path = sample_dir / "reference.wav"
sample_path.write_bytes(b"test")
user_data = {
    "voice_clone_sample_path": str(sample_path),
    "voice_clone_pending_audio": True,
    "voice_clone_pending_transcript": True,
    "voice_clone_transcript": "sample transcript",
    "voice_clone_model_id": "obsolete-model",
}

class TestMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)

message = TestMessage()
update = SimpleNamespace(
    effective_user=SimpleNamespace(id=bot.ALLOWED_TELEGRAM_USER_IDS[0]),
    message=message,
)
context = SimpleNamespace(user_data=user_data)
try:
    asyncio.run(bot.cancelclone_command(update, context))
    assert user_data == {}
    assert not sample_dir.exists()
    assert "Gemini TTS" in message.replies[0]
finally:
    shutil.rmtree(sample_root, ignore_errors=True)

print("Gemini TTS configuration and voice-clone cleanup passed; no network calls were made.")
