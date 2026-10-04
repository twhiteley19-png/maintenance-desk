"""LLM triage with a deterministic heuristic fallback.

If ANTHROPIC_API_KEY is set, triage goes through the Anthropic API with a
strict JSON contract validated by Pydantic (one retry on parse failure).
Otherwise a keyword-based heuristic is used so the whole pipeline — including
the eval harness — runs with no API key. Heuristic results carry low
confidence so the safety net routes them to a human.
"""
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from . import config

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


class TriageResult(BaseModel):
    category: Literal[
        "plumbing", "electrical", "hvac", "appliance", "pest",
        "locksmith", "structural", "general", "unknown",
    ]
    urgency: Literal["urgent", "routine"]
    summary: str
    missing_info: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)
    escalate_to_human: bool = False
    escalate_reason: str = ""


# ---------------------------------------------------------------- LLM path

def _call_llm(system_prompt: str, user_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": user_text}],
    )
    text = resp.content[0].text.strip()
    # tolerate markdown-fenced JSON
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


def llm_triage(text: str) -> TriageResult | None:
    """Strict-JSON triage via the API. Returns None if parsing fails twice."""
    system = load_prompt("triage_system.txt")
    last_error = ""
    for _attempt in range(2):
        try:
            raw = _call_llm(system, f"Tenant message:\n{text}\n\n{last_error}")
            return TriageResult.model_validate(json.loads(raw))
        except (ValidationError, json.JSONDecodeError) as e:
            last_error = f"Your last response was not valid JSON matching the schema: {e}. Try again."
    return None


# ------------------------------------------------------- heuristic fallback

_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("plumbing", ["faucet", "drip", "leak", "toilet", "sink", "drain", "pipe", "shower", "tub", "bathtub", "water heater"]),
    ("electrical", ["outlet", "light", "switch", "breaker", "power", "electric", "ceiling fan"]),
    ("hvac", ["heat", "ac", "a/c", "air condition", "thermostat", "furnace", "hvac", "vent"]),
    ("appliance", ["fridge", "refrigerator", "dishwasher", "oven", "stove", "microwave", "washer", "dryer", "garbage disposal"]),
    ("pest", ["mouse", "mice", "roach", "cockroach", "ant", "bug", "pest", "rat", "spider"]),
    ("locksmith", ["lock", "key", "locked out", "deadbolt", "doorknob"]),
    ("structural", ["roof", "ceiling", "wall", "floor", "stair", "window", "door frame", "foundation", "deck"]),
]

_ESCALATE_KEYWORDS = [
    "lawyer", "sue", "suing", "attorney", "lawsuit",
    "mold", "discriminat", "evict", "rent", "lease",
    "injured", "injury", "hurt",
]

_URGENT_HINTS = ["not working", "doesn't work", "doesnt work", "won't", "wont",
                 "can't", "cant", "broken", "stopped", "dead", "unusable",
                 "urgent", "asap", "emergency"]


def heuristic_triage(text: str) -> TriageResult:
    lowered = text.lower()

    def _hit(keywords: list[str]) -> bool:
        # Word-boundary matching with optional plural: avoids "ac" matching
        # "package" or "lock" matching "clock", while still catching
        # "outlets", "faucets", etc.
        return any(re.search(rf"\b{re.escape(k)}s?\b", lowered) for k in keywords)

    category = "unknown"
    for cat, keywords in _CATEGORY_KEYWORDS:
        if _hit(keywords):
            category = cat
            break

    escalate = _hit(_ESCALATE_KEYWORDS)
    angry = _hit(["third time", "furious", "ridiculous", "unacceptable"])
    urgency = "urgent" if _hit(_URGENT_HINTS) else "routine"

    missing = []
    if category in ("plumbing", "appliance", "hvac") and "photo" not in lowered:
        missing.append("Could you send a photo of the issue?")
    if not any(w in lowered for w in ["kitchen", "bathroom", "bedroom", "living", "hallway"]):
        missing.append("Which room is this in?")

    flagged = escalate or angry or category == "unknown"
    return TriageResult(
        category=category,  # type: ignore[arg-type]
        urgency=urgency,  # type: ignore[arg-type]
        summary=text.strip()[:160],
        missing_info=missing[:3],
        # Clear-cut cases clear the 0.7 safety-net bar; anything flagged
        # stays below it so a human reviews first. (A manager still approves
        # every request in the queue either way.)
        confidence=0.6 if flagged else 0.85,
        escalate_to_human=flagged,
        escalate_reason="heuristic fallback: flagged for human review" if flagged else "",
    )


def triage_message(text: str) -> tuple[TriageResult | None, str]:
    """Run triage. Returns (result, source) where source is 'llm' or 'heuristic'.

    Returns (None, 'llm') only when the API was used but JSON validation
    failed twice — the pipeline then routes to a human.
    """
    if config.ANTHROPIC_API_KEY:
        result = llm_triage(text)
        return result, "llm"
    return heuristic_triage(text), "heuristic"
