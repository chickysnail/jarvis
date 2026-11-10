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


def data_dir() -> Path:
    return db_path().parent


def anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def model() -> str:
    return os.environ.get("JARVIS_MODEL", "claude-sonnet-5-5")


def summary_model() -> str:
    return os.environ.get("JARVIS_SUMMARY_MODEL", "claude-haiku-5-5")


def effort() -> str:
    return os.environ.get("JARVIS_EFFORT", "medium")


def monthly_budget() -> float:
    """Optional, informational only: lets Jarvis say how much of the budget is left."""
    return float(os.environ.get("JARVIS_MONTHLY_BUDGET_USD", "0") or 0)


def heartbeat_minutes() -> int:
    return int(os.environ.get("JARVIS_HEARTBEAT_MIN", "120"))


def quiet_hours() -> tuple[int, int]:
    """Local hours [start, end) during which the heartbeat doesn't fire. Default 0-8."""
    a, b = os.environ.get("JARVIS_QUIET_HOURS", "0-8").split("-")
    return int(a), int(b)


def telegram_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("JARVIS_TZ", "Europe/Berlin"))


def port() -> int:
    return int(os.environ.get("JARVIS_PORT", "8765"))
