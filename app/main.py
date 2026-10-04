"""FastAPI front end: approval queue UI, simulate form, Twilio webhooks, JSON intake.

Phase 1:
- POST /api/messages accepts Twilio's form-encoded SMS webhook (signature
  validated) AND the original JSON shape (login-gated when a password is set).
- /voice/incoming: "please text your request" TwiML. /voice/emergency: reads
  the emergency aloud on the manager alert call.
- Single-manager auth via MANAGER_PASSWORD + signed session cookie.
- Background scheduler thread starts on server startup.
"""
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import config, pipeline, scheduler, twilio_client
from .db import SessionLocal, init_db
from .models import (
    STATUS_NEEDS_HUMAN,
    MaintenanceRequest,
    Tenant,
    Vendor,
)
from .pipeline import (
    approve_request,
    escalate_request,
    process_message,
    reject_request,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if config.MANAGER_PASSWORD:
        log.info("UI auth enabled (MANAGER_PASSWORD is set).")
    else:
        log.warning("MANAGER_PASSWORD not set — UI has no login. Local dev only.")
    if not config.twilio_configured():
        log.warning("Twilio not configured — sends are logged, not sent.")
    if config.SCHEDULER_ENABLED:
        scheduler.start_worker()
    else:
        log.info("Scheduler disabled (SCHEDULER_ENABLED=false).")
    yield


app = FastAPI(title="Maintenance Desk", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

init_db()


# ------------------------------------------------------------------ auth

def _authed(request: Request) -> bool:
    return not config.MANAGER_PASSWORD or request.session.get("manager") is True


def _login_redirect():
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if not config.MANAGER_PASSWORD:
        return RedirectResponse("/queue", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"authed": False, "error": ""})


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if config.MANAGER_PASSWORD and secrets.compare_digest(password, config.MANAGER_PASSWORD):
        request.session["manager"] = True
        return RedirectResponse("/queue", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"authed": False, "error": "Wrong password."},
        status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ UI

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/queue")


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    if not _authed(request):
        return _login_redirect()
    db = SessionLocal()
    try:
        reqs = db.query(MaintenanceRequest).order_by(MaintenanceRequest.id.desc()).all()
        return templates.TemplateResponse(
            request, "queue.html", {"requests": reqs, "authed": True})
    finally:
        db.close()


@app.get("/requests/{request_id}", response_class=HTMLResponse)
def request_detail(request: Request, request_id: int):
    if not _authed(request):
        return _login_redirect()
    db = SessionLocal()
    try:
        req = db.query(MaintenanceRequest).filter_by(id=request_id).first()
        if not req:
            raise HTTPException(404, "Request not found")
        return templates.TemplateResponse(
            request, "detail.html", {"r": req, "authed": True})
    finally:
        db.close()


@app.post("/requests/{request_id}/action")
def request_action(
    request: Request,
    request_id: int,
    action: str = Form(...),
    draft_tenant_reply: str = Form(""),
    draft_vendor_message: str = Form(""),
    note: str = Form(""),
):
    if not _authed(request):
        return _login_redirect()
    db = SessionLocal()
    try:
        req = db.query(MaintenanceRequest).filter_by(id=request_id).first()
        if not req:
            raise HTTPException(404, "Request not found")
        if action == "approve":
            # "Edit and Approve": save the manager's edits first.
            req.draft_tenant_reply = draft_tenant_reply
            req.draft_vendor_message = draft_vendor_message
            approve_request(db, req, note)
        elif action == "reject":
            reject_request(db, req, note)
        elif action == "escalate":
            escalate_request(db, req, note)
        else:
            raise HTTPException(400, "Unknown action")
        return RedirectResponse(f"/requests/{request_id}", status_code=303)
    finally:
        db.close()


@app.get("/simulate", response_class=HTMLResponse)
def simulate_form(request: Request):
    if not _authed(request):
        return _login_redirect()
    db = SessionLocal()
    try:
        tenants = db.query(Tenant).order_by(Tenant.name).all()
        return templates.TemplateResponse(
            request, "simulate.html", {"tenants": tenants, "authed": True})
    finally:
        db.close()


@app.post("/simulate")
def simulate_submit(request: Request, tenant_id: int = Form(...), body: str = Form(...)):
    if not _authed(request):
        return _login_redirect()
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if not tenant:
            raise HTTPException(404, "Tenant not found")
        req = process_message(db, tenant, body)
        return RedirectResponse(f"/requests/{req.id}", status_code=303)
    finally:
        db.close()


# ------------------------------------------------------------------ Twilio SMS webhook

def _twilio_url(request: Request) -> str:
    """The public URL Twilio called (what the signature was computed over).

    Behind a proxy the app sees an internal URL, so reconstruct the public
    one from PUBLIC_BASE_URL when it's set.
    """
    if config.PUBLIC_BASE_URL:
        qs = f"?{request.url.query}" if request.url.query else ""
        return f"{config.PUBLIC_BASE_URL}{request.url.path}{qs}"
    return str(request.url)


def _check_twilio_signature(request: Request, params: dict):
    sig = request.headers.get("x-twilio-signature", "")
    if not twilio_client.validate_signature(_twilio_url(request), params, sig):
        raise HTTPException(403, "invalid Twilio signature")


def handle_twilio_sms(db, from_phone: str, body: str, now: datetime | None = None) -> str:
    """Route an inbound SMS. Returns manager | vendor | fix-check | tenant | unknown."""
    now = now or datetime.now()
    body = body or ""

    # 1) Manager acknowledging emergencies.
    if config.MANAGER_PHONE and twilio_client.same_phone(from_phone, config.MANAGER_PHONE):
        if body.strip().upper() == "ACK":
            n = pipeline.acknowledge_emergencies(db)
            log.info("Manager ACK acknowledged %d emergencies", n)
        return "manager"

    # 2) Vendor replying to a dispatch.
    for vendor in db.query(Vendor).all():
        if twilio_client.same_phone(vendor.phone, from_phone):
            pipeline.handle_vendor_reply(db, vendor, body, now)
            return "vendor"

    # 3) Tenant: fix-check YES/NO first, otherwise a new maintenance request.
    for tenant in db.query(Tenant).all():
        if twilio_client.same_phone(tenant.phone, from_phone):
            if pipeline.handle_fix_check_reply(db, tenant, body):
                return "fix-check"
            process_message(db, tenant, body, now)
            return "tenant"

    # 4) Unknown number — flag for human handling.
    tenant = pipeline.get_or_create_unknown_tenant(db, from_phone)
    req = process_message(db, tenant, body, now)
    req.status = STATUS_NEEDS_HUMAN
    req.needs_human_reason = (
        (req.needs_human_reason + "; " if req.needs_human_reason else "")
        + f"unknown sender {from_phone} — not matched to a tenant"
    )
    pipeline.log(db, req, "system",
                 f"Unknown sender {from_phone}: flagged for human handling.")
    db.commit()
    return "unknown"


class InboundMessage(BaseModel):
    tenant_phone: str
    body: str


@app.post("/api/messages")
async def api_inbound(request: Request):
    """Intake endpoint. Accepts Twilio's form-encoded SMS webhook (signature
    validated) and the original JSON shape (login-gated when a password is set)."""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        if not _authed(request):
            raise HTTPException(401, "login required")
        msg = InboundMessage(**await request.json())
        db = SessionLocal()
        try:
            tenant = db.query(Tenant).filter_by(phone=msg.tenant_phone).first()
            if not tenant:
                raise HTTPException(
                    404, f"Unknown tenant phone {msg.tenant_phone!r} — needs human handling")
            req = process_message(db, tenant, msg.body)
            return {
                "request_id": req.id,
                "urgency": req.urgency,
                "status": req.status,
                "category": req.category,
                "needs_human_reason": req.needs_human_reason,
            }
        finally:
            db.close()

    # Twilio form-encoded webhook.
    form = await request.form()
    params = {k: v for k, v in form.items()}
    _check_twilio_signature(request, params)
    db = SessionLocal()
    try:
        handle_twilio_sms(db, params.get("From", ""), params.get("Body", ""))
    finally:
        db.close()
    return Response(content="<Response/>", media_type="text/xml")


@app.get("/api/requests")
def api_requests(request: Request):
    if not _authed(request):
        raise HTTPException(401, "login required")
    db = SessionLocal()
    try:
        reqs = db.query(MaintenanceRequest).order_by(MaintenanceRequest.id.desc()).all()
        return [
            {"id": r.id, "urgency": r.urgency, "status": r.status,
             "category": r.category, "summary": r.ai_summary}
            for r in reqs
        ]
    finally:
        db.close()


# ------------------------------------------------------------------ voice webhooks

@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    """Someone called the Twilio number: tell them to text instead."""
    form = await request.form()
    _check_twilio_signature(request, {k: v for k, v in form.items()})
    twiml = (
        "<Response>"
        '<Say voice="alice">Please text your maintenance request to this number. '
        "Goodbye.</Say>"
        "<Hangup/>"
        "</Response>"
    )
    return Response(content=twiml, media_type="text/xml")


@app.post("/voice/emergency")
async def voice_emergency(request: Request):
    """TwiML for the manager alert call: read the emergency aloud, twice."""
    form = await request.form()
    _check_twilio_signature(request, {k: v for k, v in form.items()})
    db = SessionLocal()
    try:
        req = db.query(MaintenanceRequest).filter_by(
            id=int(request.query_params.get("request_id", 0))).first()
        if req and req.tenant:
            unit = req.tenant.unit.label if req.tenant.unit else "unknown unit"
            spoken = (
                f"Maintenance emergency. {req.needs_human_reason}. "
                f"Tenant {req.tenant.name}, unit {unit}. "
                f"Reply A C K to the text message to acknowledge."
            )
        else:
            spoken = "Maintenance emergency. Check the approval queue immediately."
    finally:
        db.close()
    twice = f"{spoken} ... {spoken}"
    twiml = f'<Response><Say voice="alice">{twice}</Say><Hangup/></Response>'
    return Response(content=twiml, media_type="text/xml")
