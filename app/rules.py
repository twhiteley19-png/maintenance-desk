"""Deterministic emergency detection. Runs BEFORE any AI call.

Returns an EmergencyMatch when the message describes something that needs
a human RIGHT NOW. Otherwise returns None and the message continues to
LLM triage.

"No heat" is an emergency only when it is cold outside (configurable);
callers can override the temperature for tests.
"""
import re
from dataclasses import dataclass

from . import config

# label -> list of regexes (all case-insensitive)
_EMERGENCY_PATTERNS: list[tuple[str, list[str]]] = [
    ("gas smell", [
        r"smell(s|ing)?[^.]{0,40}\bgas\b",
        r"\bgas\b[^.]{0,40}smell(s|ing)?",
        r"\bgas\s+leak\b",
    ]),
    ("carbon monoxide", [
        r"carbon\s*monoxide",
        r"\bco\s+(alarm|detector)",
        r"carbon\s*monoxide\s+(alarm|detector)",
    ]),
    ("fire", [
        r"\bfire\b",
        r"\bflames?\b",
        r"on\s+fire",
    ]),
    ("smoke", [
        r"\bsmoke\b",
        r"\bsmoking\b",
    ]),
    ("flooding", [
        r"\bflood(ing|ed|s)?\b",
        r"water\s+everywhere",
        r"water\s+pouring",
    ]),
    ("burst pipe", [
        r"burst\s+pipe",
        r"pipe\s+burst",
        r"pipe\s+exploded",
    ]),
    ("sparking", [
        r"\bspark(s|ing|ed)?\b",
    ]),
    ("exposed wiring", [
        r"exposed\s+wir(e|ing)",
        r"bare\s+wir(e|ing)",
    ]),
    ("sewage backup", [
        r"\bsewage\b",
        r"sewer\s+backup",
        r"sewage\s+backup",
        r"toilet\s+overflowing",  # treat as sewage risk
    ]),
    ("break-in", [
        r"break[\s-]?in",
        r"\bbroke\s+in(?:to)?\b",
        r"\bintruder\b",
        r"someone\s+(is\s+)?in\s+my\s+(apartment|unit|house|place)",
    ]),
    ("injury", [
        r"\binjur(ed|y|ies)\b",
        r"\bbleeding\b",
        r"someone\s+(got\s+|was\s+)?hurt",
        r"\bi'?m\s+hurt\b",
        r"hurt\s+(badly|seriously)",
    ]),
]

_NO_HEAT_PATTERNS = [
    r"\bno\s+heat\b",
    r"heat\s+(is\s+)?not\s+working",
    r"heat\s+doesn'?t\s+work",
    r"heater\s+(is\s+)?not\s+working",
    r"furnace\s+(is\s+)?not\s+working",
    r"\bno\s+hot\s+air\b",
]


@dataclass
class EmergencyMatch:
    label: str          # e.g. "gas smell"
    matched_text: str   # the snippet that matched


def detect_emergency(text: str, outside_temp_f: int | None = None) -> EmergencyMatch | None:
    """Return the first emergency match, or None."""
    lowered = text.lower()
    for label, patterns in _EMERGENCY_PATTERNS:
        for pat in patterns:
            m = re.search(pat, lowered)
            if m:
                return EmergencyMatch(label=label, matched_text=m.group(0))

    temp = config.OUTSIDE_TEMP_F if outside_temp_f is None else outside_temp_f
    if config.NO_HEAT_EMERGENCY and temp < config.NO_HEAT_EMERGENCY_THRESHOLD_F:
        for pat in _NO_HEAT_PATTERNS:
            m = re.search(pat, lowered)
            if m:
                return EmergencyMatch(
                    label=f"no heat (outside {temp}F)",
                    matched_text=m.group(0),
                )
    return None
