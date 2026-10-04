# Maintenance Desk — Live Setup Guide

Step-by-step: everything you need to obtain, configure, and switch on to run
this live. Do the steps in order; each part builds on the last.

---

## Part 0 — What you need to gather (checklist)

- [ ] **Twilio account** (paid — trial accounts can only text verified numbers)
- [ ] **Anthropic API key** (for real AI triage + vendor-reply parsing; the
      system still works without it via the built-in heuristic fallback)
- [ ] **AppFolio API credentials** (request from your AppFolio admin portal)
- [ ] **A server that's always on** with a public HTTPS address (see Part 3)
- [ ] **Manager / on-call mobile number** for emergency SMS + voice alerts
- [ ] **Business hours** (default 8am–6pm local; used for after-hours hold)

Nothing here goes into chat or email. Secrets live in a `.env` file on the
server (Part 3). If you ever hand a secret to your assistant, use the secure
vault flow — never paste tokens into a message.

---

## Part 1 — Run it locally today (Phase 0, no accounts needed)

1. Install Python 3.11+.
2. Get the code onto the machine (`maintenance-desk.zip`).
3. Create a virtual environment and install dependencies:
   `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. Seed the demo database: `.venv/bin/python -m app.seed`
5. Sanity-check it: `.venv/bin/python -m pytest` and `.venv/bin/python eval.py`
   (expect 2 passed, 30/30 eval).
6. Start it: `.venv/bin/uvicorn app.main:app --port 8123`
7. Open `http://localhost:8123` → approval queue; `/simulate` to feed it a
   tenant message.

## Part 2 — Twilio setup (Phase 1)

1. **Create the account** at twilio.com and **upgrade from trial**. Trial
   numbers can only message numbers you've manually verified — useless live.
2. **Buy a US local number** with SMS + Voice capability (~$1.15/month).
   This is the number tenants text.
3. **A2P 10DLC registration** (required for US business texting — without it
   carriers filter or block your messages). In the Twilio console: register
   your brand, then register a messaging campaign for the number. One-time
   registration plus a small monthly campaign fee; Twilio walks you through it
   in the console.
4. **Copy two secrets** from the console dashboard: Account SID and Auth Token.
   They go into `.env` (Part 3), nowhere else.
5. **Point the number's webhooks** at your server (Part 3 gives you the URL):
   - "A MESSAGE COMES IN" → `https://YOUR-SERVER/api/messages` (HTTP POST)
   - Voice "A CALL COMES IN" → `https://YOUR-SERVER/voice/incoming`
     (plays "please text your maintenance request to this number")
6. **Test webhooks** from the Twilio console before telling any tenant the
   number.

## Part 3 — Server and configuration

The server must be reachable from the public internet over **HTTPS** —
Twilio refuses non-HTTPS webhooks. Pick one:

- **Easiest:** Render / Railway / Fly.io — connect the repo, set env vars in
  their dashboard, you get an `https://...` URL immediately.
- **Full control:** a VPS (Hetzner, DigitalOcean, etc.) with Caddy or nginx
  for HTTPS.
- **Testing only:** `ngrok http 8123` gives a temporary public URL. Fine for
  trying Twilio end-to-end; not for live use (URL changes, laptop must stay on).

**Environment variables** (`.env` file on the server, never committed):

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | AI triage + vendor-reply parsing (optional; heuristic fallback without it) |
| `TWILIO_ACCOUNT_SID` | From Twilio console |
| `TWILIO_AUTH_TOKEN` | From Twilio console |
| `TWILIO_PHONE_NUMBER` | The number you bought, e.g. `+19195551234` |
| `MANAGER_PASSWORD` | UI login password — **set this before going live** (empty = no login, local dev only) |
| `SESSION_SECRET` | Signs the login cookie — change from the default in production |
| `SCHEDULER_ENABLED` | `true` — background thread for vendor timeouts, alert repeats, fix checks, hold release |
| `EMERGENCY_ALERT_REPEAT_MINUTES` | `10` — re-send emergency SMS + voice call until the manager replies ACK |
| `VENDOR_TIMEOUT_ROUTINE_MIN` / `VENDOR_TIMEOUT_URGENT_MIN` | `30` / `10` — vendor ack window before automatic fallback |
| `FIX_CHECK_HOUR` | `10` — local hour for the next-day "is it fixed?" text |
| `FIX_CHECK_REPLY_WINDOW_HOURS` | `24` — no reply within this long → reopened as urgent, manager notified |
| `OUTSIDE_TEMP_F` | Current outside temp; drives the "no heat" emergency rule |
| `PUBLIC_BASE_URL` | `https://YOUR-SERVER` (used in webhook setup) |
| `APPFOLIO_CLIENT_ID` / `APPFOLIO_CLIENT_SECRET` / `APPFOLIO_API_BASE` | From AppFolio admin |

All of these are in `.env.example` with defaults. The server must run in the
properties' timezone (e.g. `America/New_York`) — business hours, the 10am fix
check, and the after-hours hold all use local time.

**Keep it running:** use the platform's process supervision (Render/Railway
handle this) or a systemd service on a VPS so it restarts on reboot.
**Back up** the SQLite database file nightly — it holds your tenants,
requests, and the full message log.

## Part 4 — AppFolio import (Phase 1)

1. In AppFolio, request API access for your account (admin portal).
2. Put the credentials in `.env` (`APPFOLIO_CLIENT_ID` / `APPFOLIO_CLIENT_SECRET` /
   `APPFOLIO_API_BASE`).
3. Run the import: `.venv/bin/python -m app.appfolio` — pulls tenants
   (name, phone, unit), properties, and vendors (name, phone, trade).
   Without credentials it prints what to set and changes nothing; re-running
   is safe (idempotent).
4. Verify: text the Twilio number from a tenant's real phone — the request
   should attach to the right tenant and unit automatically.
5. Re-run the import whenever tenants or vendors change (later: scheduled sync).

## Part 5 — Go-live checklist

Text the live number and confirm each path before announcing it to tenants:

- [ ] Unknown phone number → flagged for human handling, nothing misrouted
- [ ] Tenant phone, routine issue ("faucet dripping") → triaged, drafts in queue
- [ ] Approve in queue → tenant AND vendor actually receive the SMS
- [ ] Emergency text ("smell of gas") → manager gets SMS + voice call until acknowledged; reply ACK from the manager's phone stops the repeats; no AI drafts created
- [ ] Vendor replies "on my way" → dispatch marked confirmed, no fallback fires
- [ ] Vendor goes silent → fallback vendor contacted after timeout (30 min routine / 10 min urgent), tenant gets an updated "assigned" text
- [ ] Routine text at midnight → triaged and queued, held until morning (badged in the queue)
- [ ] Next-day "is it fixed? reply YES" check goes out at 10am; reply NO → reopened as urgent, manager notified

## Part 6 — What it costs (approximate, verify current pricing)

Twilio is pay-as-you-go. Rough US figures (check twilio.com/pricing before
budgeting — rates move):

| Item | Approx. cost |
|---|---|
| Phone number | ~$1.15/month |
| Outbound SMS | ~$0.008 per message |
| Inbound SMS | ~$0.008 per message |
| Outbound voice (emergency calls) | ~$0.013–0.014 per minute |
| A2P 10DLC | one-time registration + small monthly campaign fee |
| Anthropic API (triage + vendor parsing) | pennies per request; ~$1–5/month at this volume |

Illustrative month (200 tenant messages, 60 vendor dispatches, a few
emergency calls): **roughly $5–10 total.** Set a billing alert in the Twilio
console so a runaway loop can't surprise you.

## Part 7 — Security notes

- Twilio signs every webhook; the app validates the signature and rejects
  anything else. Never expose `/api/messages` without that check.
- The approval queue is the send gate — put it behind a login before it's
  internet-facing (Phase 1 includes single-manager auth).
- Rotate the Auth Token immediately if it's ever pasted anywhere visible.
