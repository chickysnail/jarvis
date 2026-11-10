import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def shared_secret() -> str:
    return os.environ.get("JARVIS_SHARED_SECRET", "")


def db_path() -> Path:
    p = Path(os.environ.get("JARVIS_DB_PATH", "data/jarvis.db"))
    return p if p.is_absolute() else ROOT / p


def host() -> str:
    return os.environ.get("JARVIS_HOST", "127.0.0.1")


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("JARVIS_TZ", "Europe/Berlin"))


def port() -> int:
    return int(os.environ.get("JARVIS_PORT", "8765"))
