"""Unit tests for the deterministic emergency rules layer."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rules import detect_emergency  # noqa: E402

# (message, outside_temp_f, expected_label_or_None)
CASES = [
    ("There is a strong smell of gas in the hallway", 70, "gas smell"),
    ("Smells like gas near the stove", 70, "gas smell"),
    ("I think there's a gas leak in the basement", 70, "gas smell"),
    ("The carbon monoxide alarm is going off", 70, "carbon monoxide"),
    ("There's a fire in the kitchen!", 70, "fire"),
    ("I see flames coming from the outlet", 70, "fire"),
    ("There's smoke coming from under the door", 70, "smoke"),
    ("My bathroom is flooding, water everywhere", 70, "flooding"),
    ("A pipe burst in the laundry room", 70, "burst pipe"),
    ("The outlet is sparking when I plug things in", 70, "sparking"),
    ("There is exposed wiring in the hallway ceiling", 70, "exposed wiring"),
    ("Sewage is backing up into the bathtub", 70, "sewage backup"),
    ("Someone broke in last night, the door is smashed", 70, "break-in"),
    ("Someone broke into my unit last night", 70, "break-in"),
    ("My kid got injured on the broken stair railing", 70, "injury"),
    ("There's no heat and it's freezing in here", 30, "no heat (outside 30F)"),
    ("The heater is not working", 20, "no heat (outside 20F)"),
    # --- negatives ---
    ("The kitchen faucet is dripping constantly", 70, None),
    ("There's no heat in the apartment", 75, None),  # warm outside: not emergency
    ("My gas bill seems high this month", 70, None),  # 'gas' alone is not a smell
    ("Can I get a parking spot?", 70, None),
    ("It's making a weird noise", 70, None),
]


def test_emergency_detection():
    failures = []
    for text, temp, expected in CASES:
        match = detect_emergency(text, outside_temp_f=temp)
        got = match.label if match else None
        if got != expected:
            failures.append(f"  {text!r}: expected {expected!r}, got {got!r}")
    assert not failures, "Rule mismatches:\n" + "\n".join(failures)


def test_mixed_emergency_and_routine():
    # Emergency must win even when bundled with a routine issue.
    match = detect_emergency(
        "Smells like gas in the hallway. Also the kitchen faucet drips.",
        outside_temp_f=70,
    )
    assert match is not None and match.label == "gas smell"
