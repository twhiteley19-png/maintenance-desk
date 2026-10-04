"""Hybrid vendor-reply parsing: keyword match first, AI only when inconclusive.

Decided 2026-09-29: deterministic fast path, LLM as the tiebreaker — the same
rules-before-AI shape as the rest of the system. Falls back to keywords alone
when no API key is set or the API call fails.
"""
import json
import re

from pydantic import BaseModel, Field, ValidationError

from . import config
from .triage import _call_llm, load_prompt


class VendorReplyParse(BaseModel):
    verdict: str = Field(pattern="^(accepted|declined|unclear)$")
    eta: str = ""  # free-text ETA if the vendor mentioned one, else ""


# (verdict, regexes) — word boundaries so "no problem" doesn't read as "no".
_ACCEPT_PATTERNS = [
    r"\bon\s+my\s+way\b",
    r"\bomw\b",
    r"\bconfirmed?\b",
    r"\byes\b",
    r"\bgot\s+it\b",
    r"\bwill\s+do\b",
    r"\bheading\s+over\b",
    r"\bbe\s+there\b",
    r"\bon\s+it\b",
]

_DECLINE_PATTERNS = [
    r"\bcan'?t\b",
    r"\btomorrow\b",
    r"\bbusy\b",
    r"\bunable\b",
    r"\bbooked\b",
    r"\bdecline[sd]?\b",
    r"\bnot\s+available\b",
    r"\bcan'?t\s+make\s+it\b",
    r"\bpass\b",
]


def keyword_parse(text: str) -> str:
    """accepted | declined | unclear — pure keyword matching."""
    lowered = text.lower()
    accept = any(re.search(p, lowered) for p in _ACCEPT_PATTERNS)
    decline = any(re.search(p, lowered) for p in _DECLINE_PATTERNS)
    if accept and not decline:
        return "accepted"
    if decline and not accept:
        return "declined"
    return "unclear"


def ai_parse(text: str) -> VendorReplyParse | None:
    """LLM parse of a vendor reply. None if it fails twice (caller treats as unclear)."""
    system = load_prompt("vendor_reply_parse.txt")
    last_error = ""
    for _attempt in range(2):
        try:
            raw = _call_llm(system, f"Vendor reply:\n{text}\n\n{last_error}")
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$", "", raw)
            return VendorReplyParse.model_validate(json.loads(raw))
        except (ValidationError, json.JSONDecodeError) as e:
            last_error = f"Your last response was not valid JSON matching the schema: {e}. Try again."
    return None


def parse_vendor_reply(text: str) -> tuple[str, str]:
    """Hybrid parse. Returns (verdict, source) where source is
    'keyword' or 'ai'. Verdict is accepted | declined | unclear."""
    verdict = keyword_parse(text)
    if verdict != "unclear":
        return verdict, "keyword"
    if config.ANTHROPIC_API_KEY:
        parsed = ai_parse(text)
        if parsed is not None:
            return parsed.verdict, "ai"
    return "unclear", "keyword"
