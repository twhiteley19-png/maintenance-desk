# Maintenance Desk — Phase 1

Tenant maintenance texts come in via Twilio, get triaged, and the system
**drafts** a tenant reply plus a vendor dispatch message. **A human approves
everything — approval is the send trigger, and nothing goes out without it.**

Phase 1 adds: real Twilio SMS in/out, emergency SMS + voice-call alerts until
the manager ACKs, vendor confirmation with automatic fallback, after-hours
hold, next-day fix checks, AppFolio read-only import, and manager login.

## Quickstart

```bash
cd maintenance-desk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# seed the demo database (2 properties, 12 units, 12 tenants, 6 vendors)
.venv/bin/python -m app.seed

# run the web UI (starts the background scheduler thread too)
.venv/bin/uvicorn app.main:app --port 8123
```

Then open:

- **http://localhost:8123/queue** — the approval queue (color-coded cards)
- **http://localhost:8123/simulate** — "simulate tenant message" form
- **POST http://localhost:8123/api/messages** — intake endpoint:
  - Twilio's form-encoded SMS webhook (`From` + `Body`, signature validated), or
  - JSON: `{"tenant_phone": "(919) 555-1001", "body": "The kitchen faucet is dripping"}`
- **POST http://localhost:8123/voice/incoming** — TwiML: "please text your maintenance request to this number"

Set `MANAGER_PASSWORD` in `.env` to put the UI behind a login (do this before
going live). Without Twilio credentials, sends are logged as "NOT SENT" so the
full flow still runs locally.

## AppFolio import (read-only)

```bash
.venv/bin/python -m app.appfolio
```

Pulls properties, units, tenants (name, phone, unit) and vendors (name, phone,
trade) using `APPFOLIO_CLIENT_ID` / `APPFOLIO_CLIENT_SECRET` /
`APPFOLIO_API_BASE`. Without credentials it prints what to set and changes
nothing. Idempotent — safe to re-run.

## How the pipeline works

```
tenant SMS → /api/messages (Twilio webhook, signature validated)
      │
      ▼
┌─────────────┐
│ RULES LAYER │  deterministic regex: gas smell, CO, fire, smoke, flooding,
│ (no AI)     │  burst pipe, sparking, exposed wiring, no-heat (if cold),
└─────────────┘  sewage, break-in, injury
      │ match?
      ├── YES → urgency=EMERGENCY, status=NEEDS_HUMAN_NOW, red banner,
      │         "CALL MANAGER NOW", AI triage skipped,
      │         SMS + voice call to MANAGER_PHONE every 10 min until ACK
      │
      ▼ NO
┌─────────────┐
│ LLM TRIAGE  │  strict JSON → Pydantic validated (1 retry, else NEEDS_HUMAN)
│ or heuristic│  category, urgency, summary, follow-up questions, confidence,
└─────────────┘  escalate flag
      │
      ▼
┌─────────────┐
│ SAFETY NET  │  confidence < 0.7 or escalate flag → NEEDS_HUMAN
└─────────────┘
      │
      ▼
 drafting (tenant reply + vendor message) → vendor picked by priority
      │
      ▼ after-hours? → triaged + drafted, HELD in queue until morning
      ▼
 approval queue → Approve / Edit+Approve / Reject / Escalate
      │
      ▼ APPROVE = SEND (via Twilio)
 tenant reply SMS + vendor dispatch SMS + tenant "assigned" SMS
      │
      ▼
 vendor ack loop (scheduler, every 60s)
   "on my way" → confirmed · "can't" → fallback · silence past timeout
   (30 min routine / 10 min urgent) → next vendor in trade → general
   handyman → back to manager
      │
      ▼ next day 10am
 "Is everything fixed? Reply YES" → YES closes out · NO / no reply in
 24h → reopened as URGENT + manager notified
```

Every step is written to `MessageLog` — inbound texts, triage notes, drafts,
approvals, sends, vendor replies, fallbacks, fix checks.

## Configuration (`.env`)

Copy `.env.example` to `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(empty)_ | Without a key, a deterministic heuristic triage is used — great for demos and evals. Set a key to use real LLM triage. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Model used for triage and drafting. |
| `OUTSIDE_TEMP_F` | `35` | "No heat" is an emergency only when this is under 40°F. |
| `NO_HEAT_EMERGENCY` | `true` | Set `false` to never treat no-heat as an emergency. |
| `DATABASE_URL` | `sqlite:///./maintenance.db` | SQLAlchemy URL. |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` | _(empty)_ | Twilio credentials + the SMS number. Without them, sends are logged as NOT SENT and the flow still runs. |
| `MANAGER_PHONE` | _(empty)_ | Mobile for emergency SMS + voice alerts (reply ACK to acknowledge) and "no vendor" / "not fixed" notices. |
| `PUBLIC_BASE_URL` | _(empty)_ | `https://your-server` — used to build webhook/voice URLs for Twilio. |
| `BUSINESS_HOURS_START` / `BUSINESS_HOURS_END` | `08:00` / `18:00` | Local-time window; routine/urgent texts outside it are held till morning. Run the server in the properties' timezone. |
| `EMERGENCY_ALERT_REPEAT_MINUTES` | `10` | Re-send emergency SMS + call until the manager ACKs. |
| `VENDOR_TIMEOUT_ROUTINE_MIN` / `VENDOR_TIMEOUT_URGENT_MIN` | `30` / `10` | Vendor ack window before fallback. |
| `FIX_CHECK_HOUR` | `10` | Local hour for the next-day "is it fixed?" text. |
| `FIX_CHECK_REPLY_WINDOW_HOURS` | `24` | No reply within this long → reopen as urgent + notify manager. |
| `MANAGER_PASSWORD` | _(empty)_ | UI login password. Empty = no auth (local dev only) — set before going live. |
| `SESSION_SECRET` | `dev-only-change-me` | Signs the login cookie. Change in production. |
| `SCHEDULER_ENABLED` | `true` | Background thread for vendor timeouts, alert repeats, fix checks, hold release. |
| `APPFOLIO_CLIENT_ID` / `APPFOLIO_CLIENT_SECRET` / `APPFOLIO_API_BASE` | _(empty)_ | Read-only AppFolio import (`python -m app.appfolio`). |

All prompts live in `prompts/` as plain text files — tune them without touching code.

## Tests & eval

```bash
.venv/bin/python -m pytest tests/ -v   # rules-layer unit tests
.venv/bin/python eval.py                # 30 sample messages through the pipeline
```

`eval.py` uses a throwaway DB and the deterministic heuristic triage, prints a
pass/fail table, and **requires 100% emergency recall** (exits non-zero otherwise).
Sample messages live in `tests/sample_messages.json`.

## Project structure

```
app/
  main.py        FastAPI app: queue UI, simulate form, Twilio/voice webhooks,
                 JSON intake, manager auth, scheduler startup
  pipeline.py    orchestration: rules → triage → safety net → drafting → queue,
                 real Twilio sends on approval, emergency alerts, vendor
                 ack/fallback, fix checks, unknown senders
  scheduler.py   background thread: vendor timeouts, emergency alert repeats,
                 fix checks, after-hours hold release (tick() is testable)
  twilio_client.py  SMS/voice wrapper + webhook signature validation
  vendor_parse.py   hybrid vendor-reply parsing (keywords → AI)
  appfolio.py    read-only AppFolio import CLI (python -m app.appfolio)
  rules.py       deterministic emergency detection
  triage.py      LLM triage (Pydantic-validated) + heuristic fallback
  drafting.py    tenant reply + vendor message drafts
  models.py      Property, Unit, Tenant, Vendor, MaintenanceRequest, MessageLog
  seed.py        demo data
  templates/     Jinja2 templates (Tailwind via CDN)
prompts/         triage_system.txt, tenant_reply.txt, vendor_message.txt,
                 vendor_reply_parse.txt
tests/           test_rules.py, test_phase1.py, sample_messages.json
eval.py          eval harness
```

## What to build next

- **AppFolio two-way sync** — push work orders back into AppFolio (import is read-only today).
- **Scheduled AppFolio sync** — re-run the import nightly instead of manually.
- **Team roles** — multiple logins with permissions (single manager today).
- **Photo handling** — accept MMS photos, attach to the request for the vendor.
- **Spend-limit guardrail** — block vendor dispatch drafts above the property's `owner_spend_limit` without explicit approval.
- **Real LLM eval** — re-run `eval.py` with `ANTHROPIC_API_KEY` set to measure triage quality with the model.
- **On-call rotation** — rotate `MANAGER_PHONE` by schedule instead of one fixed number.
