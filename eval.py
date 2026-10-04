"""Eval harness: run the 30 sample messages through the pipeline and score it.

Run:  .venv/bin/python eval.py   (from the project root)

Uses a throwaway SQLite DB and forces the deterministic heuristic triage
(no API key) so results are reproducible. Emergency recall must be 100% —
any miss is flagged loudly and the script exits non-zero.
"""
import json
import os
import sys
from pathlib import Path

# Use a throwaway DB and deterministic triage for this eval.
os.environ["DATABASE_URL"] = "sqlite:////tmp/maintenance_eval.db"
Path("/tmp/maintenance_eval.db").unlink(missing_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_HUMAN_NOW,
    MaintenanceRequest,
    Tenant,
    URGENCY_EMERGENCY,
)
from app.pipeline import process_message  # noqa: E402
from app.seed import seed  # noqa: E402

config.ANTHROPIC_API_KEY = ""   # force heuristic triage: deterministic
config.OUTSIDE_TEMP_F = 35      # cold: "no heat" is an emergency

ESCALATED_STATUSES = {STATUS_NEEDS_HUMAN, STATUS_NEEDS_HUMAN_NOW}


def main() -> int:
    init_db()
    seed()
    db = SessionLocal()
    tenants = db.query(Tenant).all()

    with open("tests/sample_messages.json") as f:
        cases = json.load(f)

    results = []
    for i, case in enumerate(cases):
        if case.get("test") == "unknown_phone":
            # The JSON intake endpoint resolves tenants by phone; an unknown
            # number must NOT match anyone (manager handles it manually).
            found = db.query(Tenant).filter_by(phone=case["phone"]).first()
            passed = found is None
            results.append({
                "id": case["id"], "text": f"[unknown phone {case['phone']}] {case['text']}",
                "expected": "no tenant match", "actual": "no tenant match" if passed else "MATCHED?!",
                "escalated": "n/a", "passed": passed, "emergency": False,
            })
            continue

        tenant = tenants[i % len(tenants)]
        req: MaintenanceRequest = process_message(db, tenant, case["text"])
        escalated = req.status in ESCALATED_STATUSES
        is_emergency = case["expected_urgency"] == URGENCY_EMERGENCY
        passed = (
            req.urgency == case["expected_urgency"]
            and escalated == case["expected_escalated"]
        )
        results.append({
            "id": case["id"], "text": case["text"],
            "expected": case["expected_urgency"], "actual": req.urgency,
            "escalated": escalated, "passed": passed, "emergency": is_emergency,
        })

    # ---- report ----
    print(f"\n{'ID':>3} | {'message':42} | {'expected':9} | {'actual':9} | {'esc':3} | result")
    print("-" * 95)
    for r in results:
        mark = "PASS" if r["passed"] else "** FAIL **"
        print(f"{r['id']:>3} | {r['text'][:42]:42} | {r['expected']:9} | {r['actual']:9} | "
              f"{str(r['escalated']):3} | {mark}")

    emergencies = [r for r in results if r["emergency"]]
    caught = [r for r in emergencies if r["actual"] == URGENCY_EMERGENCY]
    recall = len(caught) / len(emergencies) if emergencies else 1.0
    failed = [r for r in results if not r["passed"]]

    print(f"\nEmergency recall: {len(caught)}/{len(emergencies)} = {recall:.0%}")
    print(f"Overall: {len(results) - len(failed)}/{len(results)} passed")

    if recall < 1.0:
        missed = [r for r in emergencies if r["actual"] != URGENCY_EMERGENCY]
        print("\n🚨🚨🚨 EMERGENCY RECALL FAILURE — these emergencies were MISSED:")
        for r in missed:
            print(f"   #{r['id']}: {r['text']!r} -> got {r['actual']}")
        return 1
    if failed:
        print("\nNon-emergency mismatches (review):")
        for r in failed:
            print(f"   #{r['id']}: {r['text']!r}")
        return 1
    print("\nAll 30 cases passed. ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
