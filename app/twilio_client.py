"""Thin Twilio wrapper: SMS, voice calls, webhook signature validation.

Everything here is a no-op with a clear log line when Twilio isn't
configured, so the app still runs end to end locally. Tests monkeypatch
`send_sms` / `place_call` — no real network calls in tests.
"""
import logging
from typing import Optional

from twilio.request_validator import RequestValidator

from . import config

log = logging.getLogger(__name__)


def _client():
    from twilio.rest import Client

    return Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def send_sms(to: str, body: str) -> Optional[str]:
    """Send an SMS via Twilio. Returns the message SID, or None if Twilio
    isn't configured (the caller logs that case)."""
    if not config.twilio_configured():
        log.warning("Twilio not configured — SMS to %s skipped: %s", to, body[:80])
        return None
    msg = _client().messages.create(
        to=to, from_=config.TWILIO_PHONE_NUMBER, body=body
    )
    return msg.sid


def place_call(to: str, twiml_url: str) -> Optional[str]:
    """Place an outbound voice call that fetches TwiML from `twiml_url`."""
    if not config.twilio_configured():
        log.warning("Twilio not configured — voice call to %s skipped", to)
        return None
    call = _client().calls.create(to=to, from_=config.TWILIO_PHONE_NUMBER, url=twiml_url)
    return call.sid


def validate_signature(url: str, params: dict, signature: str) -> bool:
    """Validate a Twilio webhook signature. Rejects when unconfigured."""
    if not config.TWILIO_AUTH_TOKEN or not signature:
        return False
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)


def normalize_phone(phone: str) -> str:
    """Digits only, e.g. '(919) 555-1001' and '+19195551001' both -> '19195551001'."""
    return "".join(c for c in phone if c.isdigit())


def same_phone(a: str, b: str) -> bool:
    """Compare by last 10 digits so formatting/E.164 differences don't matter."""
    da, db = normalize_phone(a), normalize_phone(b)
    return len(da) >= 10 and len(db) >= 10 and da[-10:] == db[-10:]
