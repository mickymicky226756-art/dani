"""Configuration loaded from environment variables / .env file."""
import os
import re
from pathlib import Path
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Comma separated Telegram numeric user IDs (use /myid in the bot to find yours)
ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", ""))
    if x.strip().lstrip("-").isdigit()
}

BRAND_NAME = os.getenv("BRAND_NAME", "Gecho Car Lottery").strip()

# Payment details shown to customers (leave a line empty to hide it)
CBE_ACCOUNT = os.getenv("CBE_ACCOUNT", "").strip()
TELEBIRR_NUMBER = os.getenv("TELEBIRR_NUMBER", "").strip()

RESERVATION_MINUTES = _int("RESERVATION_MINUTES", 30)
MAX_NUMBERS_PER_USER = _int("MAX_NUMBERS_PER_USER", 0)  # per person per car; 0 = unlimited (default)
TZ_OFFSET_HOURS = _int("TZ_OFFSET_HOURS", 3)  # Ethiopia = UTC+3
TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Ranges are 0-99, 100-199, ... (100 wide). Telegram allows max 100 buttons per message,
# so at most 99 range buttons (+1 back button) => max 9899 tickets per car.
RANGE_SIZE = 100
MAX_TOTAL = 9899

BASE_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = BASE_DIR / "photos"
SEED_FILE = BASE_DIR / "cars_seed.json"
