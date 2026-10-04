"""Background worker: vendor ack timeouts, emergency alert repeats, fix checks,
after-hours hold release.

A plain daemon thread runs tick() every 60 seconds. tick() takes an explicit
`now` so tests can drive it with a fake clock — no sleeping, no network in
tests (twilio_client is monkeypatched there).
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from . import config

log = logging.getLogger(__name__)


def check_vendor_timeouts(db, now) -> int:
    """Fallback vendors whose ack window expired. Returns count of fallbacks."""
    from . import pipeline
    from .models import STATUS_APPROVED, MaintenanceRequest

    moved = 0
    pending = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.status == STATUS_APPROVED,
            MaintenanceRequest.vendor_dispatch_sent_at.isnot(None),
            MaintenanceRequest.vendor_confirmed == 0,
        )
        .all()
    )
    for req in pending:
        timeout_min = (
            config.VENDOR_TIMEOUT_URGENT_MIN
            if req.urgency == "URGENT"
            else config.VENDOR_TIMEOUT_ROUTINE_MIN
        )
        if req.vendor_dispatch_sent_at and now - req.vendor_dispatch_sent_at >= timedelta(
            minutes=timeout_min
        ):
            pipeline.fallback_to_next_vendor(db, req, now, reason="timeout")
            moved += 1
    return moved


def check_emergency_alerts(db, now) -> int:
    """Re-send SMS + voice call for unacknowledged emergencies."""
    from . import pipeline
    from .models import STATUS_NEEDS_HUMAN_NOW, MaintenanceRequest

    sent = 0
    open_alarms = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.status == STATUS_NEEDS_HUMAN_NOW,
            MaintenanceRequest.emergency_acknowledged == 0,
        )
        .all()
    )
    for req in open_alarms:
        due = req.last_alert_at is None or now - req.last_alert_at >= timedelta(
            minutes=config.EMERGENCY_ALERT_REPEAT_MINUTES
        )
        if due:
            pipeline.send_emergency_alert(db, req, now)
            sent += 1
    return sent


def check_fix_checks(db, now) -> int:
    """Send due next-day fix checks; reopen requests on NO / no reply."""
    from . import pipeline
    from .models import MaintenanceRequest

    acted = 0
    # 1) due and not yet sent
    due = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.fix_check_due_at.isnot(None),
            MaintenanceRequest.fix_check_due_at <= now,
            MaintenanceRequest.fix_check_sent_at.is_(None),
        )
        .all()
    )
    for req in due:
        pipeline.send_fix_check(db, req, now)
        acted += 1
    # 2) sent, no reply within the window -> reopen as urgent + tell manager
    stale = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.fix_check_sent_at.isnot(None),
            MaintenanceRequest.fix_check_reply.is_(None),
        )
        .all()
    )
    window = timedelta(hours=config.FIX_CHECK_REPLY_WINDOW_HOURS)
    for req in stale:
        if req.fix_check_sent_at and now - req.fix_check_sent_at >= window:
            pipeline.reopen_unfixed(db, req, "no reply to fix check")
            acted += 1
    return acted


def release_after_hours_holds(db, now) -> int:
    """Clear the hold flag once business hours begin."""
    from .models import MaintenanceRequest

    if not config.is_business_hours(now):
        return 0
    held = (
        db.query(MaintenanceRequest)
        .filter(MaintenanceRequest.after_hours_hold == 1)
        .all()
    )
    for req in held:
        req.after_hours_hold = 0
        # log via pipeline helper to keep MessageLog writes in one place
        from . import pipeline

        pipeline.log(db, req, "system", "Business hours began — after-hours hold released.")
    db.commit()
    return len(held)


def tick(db, now=None) -> dict:
    """Run one scheduler pass. Returns counts per check (useful in logs/tests)."""
    now = now or datetime.now()
    result = {
        "vendor_fallbacks": check_vendor_timeouts(db, now),
        "emergency_alerts": check_emergency_alerts(db, now),
        "fix_checks": check_fix_checks(db, now),
        "holds_released": release_after_hours_holds(db, now),
    }
    if any(result.values()):
        log.info("scheduler tick: %s", result)
    return result


def start_worker(interval_seconds: int = 60) -> threading.Thread:
    """Start the background daemon thread. Called once on server startup."""
    from .db import SessionLocal

    def _loop():
        while True:
            try:
                db = SessionLocal()
                try:
                    tick(db)
                finally:
                    db.close()
            except Exception:  # never let the loop die silently
                log.exception("scheduler tick failed")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="maintenance-scheduler", daemon=True)
    t.start()
    log.info("scheduler worker started (every %ss)", interval_seconds)
    return t
