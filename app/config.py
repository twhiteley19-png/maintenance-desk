"""Central configuration, all from environment with sane defaults."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root if present

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'maintenance.db'}")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Set this to the model you want to use, e.g. "claude-sonnet-4-5".
# If no ANTHROPIC_API_KEY is set, the pipeline uses a deterministic
# heuristic fallback instead of the API (good for demos and evals).
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# "No heat" is treated as an emergency only when the outside temperature
# is below this threshold (Fahrenheit). Set OUTSIDE_TEMP_F from the
# weather wherever the properties are, or flip NO_HEAT_EMERGENCY off.
NO_HEAT_EMERGENCY_THRESHOLD_F = 40
OUTSIDE_TEMP_F = int(os.getenv("OUTSIDE_TEMP_F", "35"))
NO_HEAT_EMERGENCY = os.getenv("NO_HEAT_EMERGENCY", "true").lower() == "true"

# Triage results below this confidence always go to a human.
TRIAGE_CONFIDENCE_THRESHOLD = 0.7

# ------------------------------------------------------------------ Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# Mobile that receives emergency SMS + voice-call alerts.
MANAGER_PHONE = os.getenv("MANAGER_PHONE", "")

# Public base URL of this server, e.g. https://desk.example.com
# (used to build webhook / voice URLs handed to Twilio).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def twilio_configured() -> bool:
    """True when we can actually send SMS / place calls via Twilio."""
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER)


# ------------------------------------------------------- business hours
def _parse_hhmm(value: str, default: str) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        h, m = default.split(":")
        return int(h), int(m)


BUSINESS_HOURS_START = _parse_hhmm(os.getenv("BUSINESS_HOURS_START", "08:00"), "08:00")
BUSINESS_HOURS_END = _parse_hhmm(os.getenv("BUSINESS_HOURS_END", "18:00"), "18:00")


def is_business_hours(now=None) -> bool:
    """Local-time check: is `now` inside the configured business window?

    Uses the server's local timezone — run the server in the properties'
    timezone (e.g. America/New_York).
    """
    from datetime import datetime

    now = now or datetime.now()
    t = (now.hour, now.minute)
    return BUSINESS_HOURS_START <= t < BUSINESS_HOURS_END


# ------------------------------------------------------- emergency alerts
# How often to re-send the SMS + voice call for an unacknowledged emergency.
EMERGENCY_ALERT_REPEAT_MINUTES = int(os.getenv("EMERGENCY_ALERT_REPEAT_MINUTES", "10"))

# ------------------------------------------------------- vendor fallback
# Minutes a vendor has to confirm before we move to the next one.
VENDOR_TIMEOUT_ROUTINE_MIN = int(os.getenv("VENDOR_TIMEOUT_ROUTINE_MIN", "30"))
VENDOR_TIMEOUT_URGENT_MIN = int(os.getenv("VENDOR_TIMEOUT_URGENT_MIN", "10"))

# ------------------------------------------------------- fix check
# Next-day "is it fixed?" text goes out at this local time.
FIX_CHECK_HOUR = int(os.getenv("FIX_CHECK_HOUR", "10"))
# Hours after the check with no reply before we reopen the request.
FIX_CHECK_REPLY_WINDOW_HOURS = int(os.getenv("FIX_CHECK_REPLY_WINDOW_HOURS", "24"))

# ------------------------------------------------------- auth
# Single-manager password for the UI. Empty = no auth (local dev only).
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-me")

# Set false to run the web UI without the background scheduler thread
# (useful for tests; production should leave it on).
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

# ------------------------------------------------------- AppFolio (read-only import)
APPFOLIO_CLIENT_ID = os.getenv("APPFOLIO_CLIENT_ID", "")
APPFOLIO_CLIENT_SECRET = os.getenv("APPFOLIO_CLIENT_SECRET", "")
# Base URL of the AppFolio API, e.g. https://api.appfolio.com
APPFOLIO_API_BASE = os.getenv("APPFOLIO_API_BASE", "").rstrip("/")


def appfolio_configured() -> bool:
    return bool(APPFOLIO_CLIENT_ID and APPFOLIO_CLIENT_SECRET and APPFOLIO_API_BASE)
