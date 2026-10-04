"""Phase 1 tests: Twilio webhooks, real sends, emergency alerts, vendor
fallback, fix checks, after-hours hold. No real network calls — the Twilio
client is monkeypatched and the scheduler runs on an explicit fake clock."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from twilio.request_validator import RequestValidator

os.environ["DATABASE_URL"] = "sqlite:////tmp/maintenance_phase1_test.db"
os.environ["SCHEDULER_ENABLED"] = "false"  # no background thread during tests
Path("/tmp/maintenance_phase1_test.db").unlink(missing_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, pipeline, scheduler, twilio_client  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import handle_twilio_sms  # noqa: E402
from app.models import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_NEEDS_HUMAN,
    STATUS_NEEDS_HUMAN_NOW,
    MaintenanceRequest,
    Property,
    Tenant,
    Unit,
    Vendor,
)
from app.vendor_parse import keyword_parse, parse_vendor_reply  # noqa: E402

MANAGER = "+19195559999"
TENANT_PHONE = "+19195551001"
TUE_2AM = datetime(2026, 1, 6, 2, 0)    # outside business hours
TUE_10AM = datetime(2026, 1, 6, 10, 0)  # inside business hours


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    # clean slate
    for m in (Tenant, Unit, Property, Vendor):
        session.query(m).delete()
    from app.models import MaintenanceRequest, MessageLog
    session.query(MessageLog).delete()
    session.query(MaintenanceRequest).delete()
    session.commit()

    prop = Property(name="Maple Grove", address="1200 Maple Grove Dr")
    session.add(prop)
    session.flush()
    unit = Unit(property_id=prop.id, label="1A")
    session.add(unit)
    session.flush()
    session.add(Tenant(unit_id=unit.id, name="Alicia Gomez", phone=TENANT_PHONE))
    session.add(Vendor(trade="plumbing", name="Plumb A", phone="+19195550101", priority=1))
    session.add(Vendor(trade="plumbing", name="Plumb B", phone="+19195550102", priority=2))
    session.add(Vendor(trade="general", name="Handy G", phone="+19195550103", priority=1))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def sms(monkeypatch):
    """Capture outbound SMS / calls instead of hitting Twilio."""
    sent = []
    calls = []
    monkeypatch.setattr(twilio_client, "send_sms",
                        lambda to, body: sent.append((to, body)) or "SMxxx")
    monkeypatch.setattr(twilio_client, "place_call",
                        lambda to, url: calls.append((to, url)) or "CAxxx")
    monkeypatch.setattr(config, "MANAGER_PHONE", MANAGER)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://example.com")
    return sent, calls


def _tenant(db):
    return db.query(Tenant).filter_by(phone=TENANT_PHONE).first()


# ------------------------------------------------- signature validation

def test_twilio_signature_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "testtoken")
    url = "https://example.com/api/messages"
    params = {"From": TENANT_PHONE, "Body": "faucet dripping"}
    sig = RequestValidator("testtoken").compute_signature(url, params)
    assert twilio_client.validate_signature(url, params, sig) is True
    assert twilio_client.validate_signature(url, params, "bogus") is False
    assert twilio_client.validate_signature(url, {"From": TENANT_PHONE}, sig) is False


def test_twilio_signature_rejected_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    assert twilio_client.validate_signature("https://x", {}, "anything") is False


# ------------------------------------------------- after-hours hold

def test_after_hours_hold_and_release(db):
    tenant = _tenant(db)
    night = pipeline.process_message(db, tenant, "The kitchen faucet is dripping", now=TUE_2AM)
    assert night.after_hours_hold == 1
    assert night.status == "NEW"  # triaged + drafted, just held
    assert "held in queue until morning" in night.log[-1].body

    day = pipeline.process_message(db, tenant, "The kitchen faucet is dripping", now=TUE_10AM)
    assert day.after_hours_hold == 0

    released = scheduler.release_after_hours_holds(db, datetime(2026, 1, 6, 8, 0))
    assert released == 1
    db.refresh(night)
    assert night.after_hours_hold == 0


def test_emergency_ignores_after_hours(db, sms):
    tenant = _tenant(db)
    req = pipeline.process_message(db, tenant, "strong smell of gas in hallway", now=TUE_2AM)
    assert req.status == STATUS_NEEDS_HUMAN_NOW
    assert req.after_hours_hold == 0
    sent, calls = sms
    assert any(t == MANAGER for t, _ in sent)  # SMS alert went out
    assert len(calls) == 1  # voice call placed


# ------------------------------------------------- emergency alert + ACK

def test_emergency_alert_repeats_until_ack(db, sms):
    tenant = _tenant(db)
    req = pipeline.process_message(db, tenant, "strong smell of gas", now=TUE_10AM)
    sent, _ = sms
    assert len([t for t, _ in sent if t == MANAGER]) == 1

    # 5 minutes later: no repeat yet
    assert scheduler.check_emergency_alerts(db, TUE_10AM + timedelta(minutes=5)) == 0
    # 11 minutes later: repeats
    assert scheduler.check_emergency_alerts(db, TUE_10AM + timedelta(minutes=11)) == 1
    assert len([t for t, _ in sent if t == MANAGER]) == 2

    # Manager ACKs -> alerts stop
    assert handle_twilio_sms(db, MANAGER, "ACK", now=TUE_10AM) == "manager"
    db.refresh(req)
    assert req.emergency_acknowledged == 1
    assert scheduler.check_emergency_alerts(db, TUE_10AM + timedelta(hours=2)) == 0


# ------------------------------------------------- approve sends for real

def test_approve_sends_tenant_vendor_and_assigned(db, sms):
    tenant = _tenant(db)
    req = pipeline.process_message(db, tenant, "The kitchen faucet is dripping", now=TUE_10AM)
    pipeline.approve_request(db, req, now=TUE_10AM)
    sent, _ = sms
    to_numbers = [t for t, _ in sent]
    assert to_numbers.count(TENANT_PHONE) == 2  # tenant reply + assigned update
    assert "+19195550101" in to_numbers  # vendor dispatch to Plumb A
    assert any("assigned" in b for t, b in sent if t == TENANT_PHONE)
    db.refresh(req)
    assert req.vendor_dispatch_sent_at == TUE_10AM
    assert req.fix_check_due_at == datetime(2026, 1, 7, 10, 0)  # next day 10am


# ------------------------------------------------- vendor fallback

def _approve_plumbing(db):
    tenant = _tenant(db)
    req = pipeline.process_message(db, tenant, "The kitchen faucet is dripping", now=TUE_10AM)
    pipeline.approve_request(db, req, now=TUE_10AM)
    return req


def test_vendor_timeout_fallback_order(db, sms):
    req = _approve_plumbing(db)
    sent, _ = sms
    assert req.chosen_vendor.name == "Plumb A"

    # 29 min: still waiting
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=29)) == 0
    # 31 min: falls back to Plumb B (same trade, next priority)
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=31)) == 1
    db.refresh(req)
    assert req.chosen_vendor.name == "Plumb B"
    assert any(t == "+19195550102" for t, _ in sent)

    # another 31 min: falls back to Handy G (general trade)
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=62)) == 1
    db.refresh(req)
    assert req.chosen_vendor.name == "Handy G"

    # another 31 min: nobody left -> back to manager
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=93)) == 1
    db.refresh(req)
    assert req.status == STATUS_NEEDS_HUMAN
    assert "all vendors tried" in req.needs_human_reason
    assert any(t == MANAGER for t, _ in sent)


def test_vendor_decline_triggers_fallback(db, sms):
    req = _approve_plumbing(db)
    assert handle_twilio_sms(db, "+19195550101", "can't make it today", now=TUE_10AM) == "vendor"
    db.refresh(req)
    assert req.chosen_vendor.name == "Plumb B"


def test_vendor_accept_confirms(db, sms):
    req = _approve_plumbing(db)
    assert handle_twilio_sms(db, "+19195550101", "on my way", now=TUE_10AM) == "vendor"
    db.refresh(req)
    assert req.vendor_confirmed == 1
    # no fallback fires afterwards
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(hours=5)) == 0


def test_fallback_readdresses_dispatch_to_new_vendor(db, sms):
    req = _approve_plumbing(db)
    sent, _ = sms
    assert "Plumb A" in req.draft_vendor_message
    scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=31))
    to_b = [b for t, b in sent if t == "+19195550102"]
    assert to_b, "no dispatch sent to Plumb B"
    assert "Plumb B" in to_b[-1] and "Plumb A" not in to_b[-1]


def test_urgent_timeout_is_10_minutes(db, sms):
    tenant = _tenant(db)
    req2 = pipeline.process_message(db, tenant, "The toilet won't flush at all, broken", now=TUE_10AM)
    assert req2.urgency == "URGENT"
    pipeline.approve_request(db, req2, now=TUE_10AM)
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=9)) == 0
    assert scheduler.check_vendor_timeouts(db, TUE_10AM + timedelta(minutes=11)) == 1


# ------------------------------------------------- hybrid reply parsing

@pytest.mark.parametrize("text,expected", [
    ("on my way", "accepted"),
    ("OMW, be there in 20", "accepted"),
    ("Confirmed, got it", "accepted"),
    ("yes", "accepted"),
    ("can't make it today", "declined"),
    ("Sorry, booked solid", "declined"),
    ("tomorrow morning works", "declined"),
    ("no problem, on it", "accepted"),  # "no" inside "no problem" must not decline
    ("stuck on 540, be there in 40", "accepted"),
    ("let me check my schedule", "unclear"),  # no keyword hit
    ("what's the address?", "unclear"),
])
def test_keyword_parse(text, expected):
    assert keyword_parse(text) == expected


def test_hybrid_falls_back_to_unclear_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    verdict, source = parse_vendor_reply("let me check my schedule")
    assert (verdict, source) == ("unclear", "keyword")
    verdict, source = parse_vendor_reply("on my way")
    assert (verdict, source) == ("accepted", "keyword")


# ------------------------------------------------- fix check

def test_fix_check_yes_closes_out(db, sms):
    req = _approve_plumbing(db)
    sent, _ = sms
    # next day 10am: check goes out
    due = datetime(2026, 1, 7, 10, 0)
    assert scheduler.check_fix_checks(db, due) == 1
    assert any("is everything fixed" in b for t, b in sent if t == TENANT_PHONE)
    # tenant replies YES
    assert handle_twilio_sms(db, TENANT_PHONE, "YES", now=due) == "fix-check"
    db.refresh(req)
    assert req.fix_check_reply == "YES"


def test_fix_check_no_reopens(db, sms):
    req = _approve_plumbing(db)
    sent, _ = sms
    due = datetime(2026, 1, 7, 10, 0)
    scheduler.check_fix_checks(db, due)
    assert handle_twilio_sms(db, TENANT_PHONE, "NO", now=due) == "fix-check"
    db.refresh(req)
    assert req.fix_check_reply == "NO"
    assert req.status == STATUS_NEEDS_HUMAN
    assert req.urgency == "URGENT"
    assert any(t == MANAGER for t, _ in sent)


def test_fix_check_no_reply_reopens(db, sms):
    req = _approve_plumbing(db)
    due = datetime(2026, 1, 7, 10, 0)
    scheduler.check_fix_checks(db, due)
    # 25h later, still no reply -> reopen
    assert scheduler.check_fix_checks(db, due + timedelta(hours=25)) == 1
    db.refresh(req)
    assert req.status == STATUS_NEEDS_HUMAN
    assert "no reply to fix check" in req.needs_human_reason


# ------------------------------------------------- unknown sender

def test_unknown_sender_flagged(db, sms):
    result = handle_twilio_sms(db, "+19195557777", "my sink is leaking", now=TUE_10AM)
    assert result == "unknown"
    tenant = db.query(Tenant).filter(Tenant.phone == "+19195557777").first()
    assert tenant is not None and "Unknown" in tenant.name
    req = tenant.requests[0]
    assert req.status == STATUS_NEEDS_HUMAN
    assert "unknown sender" in req.needs_human_reason


# ------------------------------------------------- phone normalization

def test_same_phone_formats():
    assert twilio_client.same_phone("(919) 555-1001", "+19195551001")
    assert not twilio_client.same_phone("(919) 555-1001", "(919) 555-1002")


# ------------------------------------------------- HTTP endpoint wiring

def _sig(url: str, params: dict) -> str:
    return RequestValidator("testtoken").compute_signature(url, params)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")  # validate against testserver URL
    monkeypatch.setattr(config, "MANAGER_PHONE", MANAGER)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_sms_webhook_end_to_end(db, sms, client, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_PASSWORD", "")  # webhook needs no login
    params = {"From": TENANT_PHONE, "Body": "The kitchen faucet is dripping",
              "MessageSid": "SM1"}
    url = "http://testserver/api/messages"
    r = client.post("/api/messages", data=params,
                    headers={"X-Twilio-Signature": _sig(url, params)})
    assert r.status_code == 200
    assert "<Response/>" in r.text
    req = db.query(MaintenanceRequest).order_by(MaintenanceRequest.id.desc()).first()
    assert req is not None and "faucet" in req.raw_message


def test_sms_webhook_rejects_bad_signature(db, client):
    params = {"From": TENANT_PHONE, "Body": "hi"}
    r = client.post("/api/messages", data=params,
                    headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403


def test_voice_incoming_twiml(db, client):
    params = {"From": "+19195550000", "CallSid": "CA1"}
    url = "http://testserver/voice/incoming"
    r = client.post("/voice/incoming", data=params,
                    headers={"X-Twilio-Signature": _sig(url, params)})
    assert r.status_code == 200
    assert "Please text your maintenance request to this number" in r.text
    assert "<Hangup/>" in r.text


def test_voice_incoming_rejects_bad_signature(db, client):
    r = client.post("/voice/incoming", data={"From": "x"},
                    headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403


def test_login_flow(db, client, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_PASSWORD", "s3cret")
    # queue requires login
    r = client.get("/queue", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # wrong password
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401
    # right password -> in
    r = client.post("/login", data={"password": "s3cret"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/queue")
    assert r.status_code == 200 and "Approval queue" in r.text
    # logout -> locked out again
    client.get("/logout")
    r = client.get("/queue", follow_redirects=False)
    assert r.status_code == 303


def test_json_intake_without_password(db, client, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_PASSWORD", "")
    r = client.post("/api/messages",
                    json={"tenant_phone": TENANT_PHONE, "body": "faucet dripping"})
    assert r.status_code == 200
    assert r.json()["category"] == "plumbing"
