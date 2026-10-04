"""Pipeline orchestration: rules -> triage -> safety net -> drafting -> queue.

Phase 1: approval SENDS via Twilio (tenant reply + vendor dispatch).
Approval is still the send trigger — nothing goes out without it.

New in Phase 1:
- emergency alerts: SMS + voice call to MANAGER_PHONE until ACK
- after-hours hold for routine/urgent (triaged + drafted, held till morning)
- vendor ack/fallback loop (timeouts, hybrid reply parsing)
- tenant "assigned" SMS on vendor dispatch, next-day fix check
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import config, twilio_client
from .drafting import draft_tenant_reply, draft_vendor_message
from .models import (
    STATUS_APPROVED,
    STATUS_ESCALATED,
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_HUMAN_NOW,
    STATUS_NEW,
    STATUS_REJECTED,
    URGENCY_EMERGENCY,
    URGENCY_ROUTINE,
    URGENCY_URGENT,
    MaintenanceRequest,
    MessageLog,
    Property,
    Tenant,
    Unit,
    Vendor,
)
from .rules import detect_emergency
from .triage import triage_message
from .vendor_parse import parse_vendor_reply


def log(db: Session, request: MaintenanceRequest, direction: str, body: str):
    entry = MessageLog(request_id=request.id, direction=direction, body=body)
    db.add(entry)
    db.flush()
    return entry


def choose_vendor(db: Session, category: str) -> Vendor | None:
    vendor = (
        db.query(Vendor)
        .filter(Vendor.trade == category)
        .order_by(Vendor.priority, Vendor.id)
        .first()
    )
    if vendor is None and category != "general":
        vendor = (
            db.query(Vendor)
            .filter(Vendor.trade == "general")
            .order_by(Vendor.priority, Vendor.id)
            .first()
        )
    return vendor


def process_message(
    db: Session, tenant: Tenant, raw_message: str, now: datetime | None = None
) -> MaintenanceRequest:
    """Run one tenant message through the full pipeline."""
    now = now or datetime.now()
    req = MaintenanceRequest(
        tenant_id=tenant.id,
        raw_message=raw_message,
        urgency=URGENCY_ROUTINE,
        status=STATUS_NEW,
        created_at=now,
    )
    db.add(req)
    db.flush()
    log(db, req, "in", f"Tenant {tenant.name} ({tenant.phone}): {raw_message}")

    # 1) Rules layer first — deterministic, no AI.
    emergency = detect_emergency(raw_message)
    if emergency:
        req.urgency = URGENCY_EMERGENCY
        req.status = STATUS_NEEDS_HUMAN_NOW
        req.category = "unknown"
        req.needs_human_reason = f"Emergency keyword matched: '{emergency.label}'"
        req.ai_summary = f"EMERGENCY: {emergency.label} reported by tenant."
        log(db, req, "system",
            f"EMERGENCY DETECTED ({emergency.label}). AI triage skipped. "
            f"Awaiting manager. CALL MANAGER NOW.")
        db.commit()
        print(f"\n!!! CALL MANAGER NOW — emergency '{emergency.label}' "
              f"reported by {tenant.name} (request #{req.id}) !!!\n")
        send_emergency_alert(db, req, now)
        return req

    # 2) Triage (LLM when configured, heuristic otherwise).
    result, source = triage_message(raw_message)
    log(db, req, "system", f"Triage via {source}: {result}")

    if result is None:
        req.status = STATUS_NEEDS_HUMAN
        req.needs_human_reason = "Triage failed to return valid JSON after retry."
        log(db, req, "system", "Triage parse failure -> NEEDS_HUMAN")
        db.commit()
        return req

    req.category = result.category
    req.urgency = URGENCY_URGENT if result.urgency == "urgent" else URGENCY_ROUTINE
    req.ai_summary = result.summary
    req.follow_up_questions = "\n".join(result.missing_info)

    # 3) Safety net.
    reasons = []
    if result.confidence < config.TRIAGE_CONFIDENCE_THRESHOLD:
        reasons.append(f"low triage confidence ({result.confidence:.2f})")
    if result.escalate_to_human:
        reasons.append(result.escalate_reason or "triage flagged for human review")
    if reasons:
        req.status = STATUS_NEEDS_HUMAN
        req.needs_human_reason = "; ".join(reasons)

    # 4) Drafting (drafts only — human approves).
    req.draft_tenant_reply = draft_tenant_reply(
        tenant.name, result.summary, result.missing_info)
    log(db, req, "out", f"DRAFT tenant reply (not sent):\n{req.draft_tenant_reply}")

    vendor = choose_vendor(db, result.category)
    if vendor:
        req.chosen_vendor_id = vendor.id
        unit_label = tenant.unit.label if tenant.unit else "?"
        prop = tenant.unit.property if tenant.unit else None
        req.draft_vendor_message = draft_vendor_message(
            vendor.name, vendor.trade,
            prop.name if prop else "?", prop.address if prop else "?",
            unit_label, result.summary)
        log(db, req, "out",
            f"DRAFT vendor message to {vendor.name} (not sent):\n{req.draft_vendor_message}")
    else:
        req.needs_human_reason += "; no vendor found for category"
        req.status = STATUS_NEEDS_HUMAN

    # 5) After-hours hold: triaged + drafted now, manager acts in the morning.
    # Emergencies never reach here (handled above) and alert 24/7.
    if not config.is_business_hours(now):
        req.after_hours_hold = 1
        log(db, req, "system",
            "Received outside business hours — triaged and drafted, "
            "held in queue until morning.")

    db.commit()
    return req


# ------------------------------------------------- emergency alerting

def send_emergency_alert(db: Session, req: MaintenanceRequest, now: datetime | None = None):
    """SMS + voice call to MANAGER_PHONE. Repeats until ACK (see scheduler)."""
    now = now or datetime.now()
    if not config.MANAGER_PHONE:
        log(db, req, "system", "No MANAGER_PHONE configured — emergency alert skipped.")
        db.commit()
        return
    tenant = req.tenant
    unit_label = tenant.unit.label if tenant.unit else "?"
    body = (
        f"🚨 MAINTENANCE EMERGENCY: {req.needs_human_reason}. "
        f"Tenant: {tenant.name} ({tenant.phone}), Unit {unit_label}. "
        f"Reply ACK to acknowledge."
    )
    sid = twilio_client.send_sms(config.MANAGER_PHONE, body)
    if sid:
        log(db, req, "out", f"Emergency SMS sent to manager (sid={sid}).")
    else:
        log(db, req, "out", f"Emergency SMS NOT SENT (Twilio not configured):\n{body}")
    twiml_url = (
        f"{config.PUBLIC_BASE_URL}/voice/emergency?request_id={req.id}"
        if config.PUBLIC_BASE_URL else ""
    )
    if twiml_url:
        call_sid = twilio_client.place_call(config.MANAGER_PHONE, twiml_url)
        if call_sid:
            log(db, req, "out", f"Emergency voice call placed (sid={call_sid}).")
    req.last_alert_at = now
    db.commit()


def acknowledge_emergencies(db: Session) -> int:
    """Manager replied ACK: mark all open emergencies acknowledged."""
    open_alarms = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.status == STATUS_NEEDS_HUMAN_NOW,
            MaintenanceRequest.emergency_acknowledged == 0,
        )
        .all()
    )
    for req in open_alarms:
        req.emergency_acknowledged = 1
        log(db, req, "system", "Manager acknowledged emergency via ACK.")
    db.commit()
    return len(open_alarms)


# ------------------------------------------------- approval queue actions

def _send_or_log(db, req, to: str, body: str, label: str):
    sid = twilio_client.send_sms(to, body)
    if sid:
        log(db, req, "out", f"SENT via Twilio to {label} (sid={sid}):\n{body}")
    else:
        log(db, req, "out", f"NOT SENT (Twilio not configured) to {label}:\n{body}")
    return sid


def approve_request(
    db: Session, req: MaintenanceRequest, note: str = "", now: datetime | None = None
) -> MaintenanceRequest:
    now = now or datetime.now()
    req.status = STATUS_APPROVED
    req.after_hours_hold = 0  # manager acted — hold no longer applies
    log(db, req, "system", f"APPROVED by manager. {note}".strip())

    # Approval is the send trigger: tenant reply goes out.
    if req.draft_tenant_reply and req.tenant.phone:
        _send_or_log(db, req, req.tenant.phone, req.draft_tenant_reply, "tenant")

    # ...and the vendor dispatch goes out, starting the ack/fallback loop.
    if req.draft_vendor_message and req.chosen_vendor:
        vendor = req.chosen_vendor
        _send_or_log(db, req, vendor.phone, req.draft_vendor_message,
                     f"vendor {vendor.name}")
        req.vendor_dispatch_sent_at = now
        req.tried_vendor_ids = str(vendor.id)
        req.vendor_confirmed = 0
        req.fallback_count = 0
        send_tenant_assigned_sms(db, req, vendor)

        # Next-day fix check: "Is everything fixed? Reply YES" at 10am.
        due = (now + timedelta(days=1)).replace(
            hour=config.FIX_CHECK_HOUR, minute=0, second=0, microsecond=0)
        req.fix_check_due_at = due
        log(db, req, "system", f"Fix check scheduled for {due:%m/%d %H:%M}.")

    db.commit()
    return req


def send_tenant_assigned_sms(db: Session, req: MaintenanceRequest, vendor: Vendor):
    """Tell the tenant a vendor is on the job. Sent automatically on dispatch
    (approval was the human gate) and on every fallback to a new vendor."""
    body = (
        f"Update from Property Management: {vendor.name} has been assigned "
        f"to your request ({req.ai_summary or req.category}). "
        f"They'll contact you about access."
    )
    _send_or_log(db, req, req.tenant.phone, body, "tenant (assigned update)")


def reject_request(db: Session, req: MaintenanceRequest, note: str = "") -> MaintenanceRequest:
    req.status = STATUS_REJECTED
    log(db, req, "system", f"REJECTED by manager. {note}".strip())
    db.commit()
    return req


def escalate_request(db: Session, req: MaintenanceRequest, note: str = "") -> MaintenanceRequest:
    req.status = STATUS_ESCALATED
    log(db, req, "system", f"ESCALATED by manager for personal handling. {note}".strip())
    db.commit()
    return req


# ------------------------------------------------- vendor ack / fallback

def handle_vendor_reply(db: Session, vendor: Vendor, body: str,
                        now: datetime | None = None) -> str:
    """Route a vendor's SMS reply. Returns accepted | declined | unclear | no-op."""
    now = now or datetime.now()
    req = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.chosen_vendor_id == vendor.id,
            MaintenanceRequest.status == STATUS_APPROVED,
            MaintenanceRequest.vendor_dispatch_sent_at.isnot(None),
            MaintenanceRequest.vendor_confirmed == 0,
        )
        .order_by(MaintenanceRequest.vendor_dispatch_sent_at.desc())
        .first()
    )
    if not req:
        return "no-op"
    log(db, req, "in", f"Vendor {vendor.name} ({vendor.phone}): {body}")
    verdict, source = parse_vendor_reply(body)
    log(db, req, "system", f"Vendor reply parsed via {source}: {verdict}")
    if verdict == "accepted":
        req.vendor_confirmed = 1
        log(db, req, "system", f"Vendor {vendor.name} confirmed the job.")
    elif verdict == "declined":
        fallback_to_next_vendor(db, req, now, reason="vendor declined")
    else:
        log(db, req, "system",
            "Vendor reply unclear — waiting for the ack timeout before fallback.")
    db.commit()
    return verdict


def fallback_to_next_vendor(db: Session, req: MaintenanceRequest,
                            now: datetime, reason: str):
    """Move the dispatch to the next vendor: same trade by priority, then
    general handyman, then back to the manager."""
    tried = {v for v in (req.tried_vendor_ids or "").split(",") if v}
    if req.chosen_vendor_id:
        tried.add(str(req.chosen_vendor_id))

    def _next(trade: str):
        q = db.query(Vendor).filter(Vendor.trade == trade).order_by(Vendor.priority, Vendor.id)
        return next((v for v in q if str(v.id) not in tried), None)

    nxt = _next(req.category) or _next("general")
    if nxt is None:
        req.status = STATUS_NEEDS_HUMAN
        req.needs_human_reason = (
            f"No vendor confirmed ({reason}); all vendors tried. Manager action needed.")
        log(db, req, "system", req.needs_human_reason)
        if config.MANAGER_PHONE:
            _send_or_log(db, req, config.MANAGER_PHONE,
                         f"Maintenance Desk: no vendor confirmed for request #{req.id} "
                         f"({req.ai_summary or req.category}). {reason}. Please handle manually.",
                         "manager")
        db.commit()
        return

    old_vendor = req.chosen_vendor
    req.chosen_vendor_id = nxt.id
    req.tried_vendor_ids = ",".join(sorted(tried | {str(nxt.id)}))
    req.fallback_count = (req.fallback_count or 0) + 1
    req.vendor_confirmed = 0
    req.vendor_dispatch_sent_at = now
    log(db, req, "system",
        f"Fallback ({reason}) → {nxt.name} (attempt #{req.fallback_count + 1}).")
    # Re-address the approved dispatch text to the new vendor (the manager
    # approved the content; only the recipient changes).
    dispatch = req.draft_vendor_message
    if old_vendor and old_vendor.name in dispatch:
        dispatch = dispatch.replace(old_vendor.name, nxt.name)
    _send_or_log(db, req, nxt.phone, dispatch, f"vendor {nxt.name}")
    send_tenant_assigned_sms(db, req, nxt)
    db.commit()


# ------------------------------------------------- fix check

def send_fix_check(db: Session, req: MaintenanceRequest, now: datetime | None = None):
    now = now or datetime.now()
    body = (
        f"Hi {req.tenant.name}, this is Property Management following up: "
        f"is everything fixed? Reply YES."
    )
    _send_or_log(db, req, req.tenant.phone, body, "tenant (fix check)")
    req.fix_check_sent_at = now
    db.commit()


def handle_fix_check_reply(db: Session, tenant: Tenant, body: str) -> bool:
    """Handle a tenant's YES/NO to an outstanding fix check. Returns True if
    the message was consumed as a fix-check reply (not a new request)."""
    req = (
        db.query(MaintenanceRequest)
        .filter(
            MaintenanceRequest.tenant_id == tenant.id,
            MaintenanceRequest.fix_check_sent_at.isnot(None),
            MaintenanceRequest.fix_check_reply.is_(None),
        )
        .order_by(MaintenanceRequest.fix_check_sent_at.desc())
        .first()
    )
    if not req:
        return False
    answer = body.strip().upper()
    if answer not in ("YES", "Y", "NO", "N"):
        return False
    log(db, req, "in", f"Tenant {tenant.name} fix-check reply: {body.strip()}")
    if answer in ("YES", "Y"):
        req.fix_check_reply = "YES"
        log(db, req, "system", "Tenant confirmed the fix. Request closed out.")
    else:
        req.fix_check_reply = "NO"
        reopen_unfixed(db, req, "tenant replied NO to fix check")
    db.commit()
    return True


def reopen_unfixed(db: Session, req: MaintenanceRequest, reason: str):
    """Reopen a request as urgent and notify the manager."""
    req.status = STATUS_NEEDS_HUMAN
    req.urgency = URGENCY_URGENT
    req.needs_human_reason = f"Fix not confirmed: {reason}."
    log(db, req, "system", req.needs_human_reason)
    if config.MANAGER_PHONE:
        _send_or_log(db, req, config.MANAGER_PHONE,
                     f"Maintenance Desk: request #{req.id} ({req.ai_summary or req.category}) "
                     f"for {req.tenant.name} is NOT fixed ({reason}). Please follow up.",
                     "manager")
    db.commit()


# ------------------------------------------------- unknown senders

def get_or_create_unknown_tenant(db: Session, phone: str) -> Tenant:
    """A text from a number matching no tenant/vendor: park it under a
    sentinel tenant so it shows in the queue flagged for human handling."""
    for t in db.query(Tenant).all():
        if twilio_client.same_phone(t.phone, phone):
            return t
    prop = db.query(Property).filter_by(name="Unknown").first()
    if not prop:
        prop = Property(name="Unknown", address="—")
        db.add(prop)
        db.flush()
    unit = db.query(Unit).filter_by(property_id=prop.id, label="Unassigned").first()
    if not unit:
        unit = Unit(property_id=prop.id, label="Unassigned")
        db.add(unit)
        db.flush()
    tenant = Tenant(unit_id=unit.id, name=f"Unknown caller {phone}", phone=phone)
    db.add(tenant)
    db.flush()
    return tenant
