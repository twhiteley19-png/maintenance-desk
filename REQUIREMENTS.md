# Maintenance Desk — Functional Requirements

Agreed 2026-09-29. This is the reference for all future builds.
Phase 0 (built): triage + drafting + approval queue, mock sends only.

## Product principles (locked)

- **Human approves everything.** Nothing is sent to a tenant or vendor without
  manager approval. Approval is the send trigger.
- **Emergency rules run deterministically before AI.** On a match, AI
  triage/drafting is skipped entirely.
- AI never commits to spend, prices, arrival times, fault, or "safe to ignore."
- AI never discusses eviction, rent disputes, lease terms, screening, or legal
  matters — those escalate to a human.
- Complete message log: every inbound message, draft, approval, and send.

## Intake

- Channel: **SMS via Twilio** (Phase 1). Tenants text the Twilio number.
- Inbound phone number is matched to a tenant record (via AppFolio import).
  Unknown numbers are flagged for human handling.
- Voice calls are **out of scope**: callers hear "please text your maintenance
  request to this number." No voicemail transcription (revisit later if needed).

## Triage pipeline (unchanged from Phase 0)

rules → triage (LLM, heuristic fallback) → confidence/escalation safety net →
drafts → approval queue.

## Emergency handling

- On a rules match: status `NEEDS_HUMAN_NOW`, red banner in UI, no AI drafting.
- **Alerting:** SMS + automated voice call to the manager/on-call number,
  repeating every `EMERGENCY_ALERT_REPEAT_MINUTES` (default 10) until acknowledged.
- **Acknowledgment:** the manager replies **ACK** to the alert SMS (decided 2026-09-29).
  One ACK clears all currently open emergencies.
- "CALL MANAGER NOW" retained in logs and UI.
- On-call number is `MANAGER_PHONE` in env (rotation is a later phase).

## Approval and sending (Phase 1)

- Approve / Edit+Approve in the queue **actually sends via Twilio**:
  tenant reply SMS + vendor dispatch SMS.
- Reject / Escalate behavior unchanged.
- **Tenant status update:** approving a vendor dispatch also sends the tenant
  an "assigned" SMS automatically (the approval itself is the human gate).
- **Next-day completion check:** automated "Is everything fixed? Reply YES"
  SMS to the tenant at `FIX_CHECK_HOUR` (default 10am local) the next day.
- On **NO**, or no reply within `FIX_CHECK_REPLY_WINDOW_HOURS` (default 24h):
  the request reopens as **urgent** (`NEEDS_HUMAN`) and the manager gets an SMS.
  On **YES**, the request is closed out. (Decided 2026-09-29.)

## Vendor acknowledgment and fallback

- After a vendor dispatch is sent, the vendor must confirm.
- **Timeouts:** 30 minutes for routine, 10 minutes for urgent.
- **Fallback order:** next vendor in the same trade → general handyman →
  back to the manager. Decline and timeout both trigger fallback.
- **Reply parsing: hybrid (decided 2026-09-29).** Keyword match first as the
  fast path; AI parse only when keywords are inconclusive. Falls back to
  keywords if no API key / API failure.

## After-hours behavior

- Emergencies: alert immediately, 24/7.
- Routine/urgent: triaged and drafted immediately, held in the approval queue
  until morning. Business hours are `BUSINESS_HOURS_START`/`BUSINESS_HOURS_END`
  (default 08:00–18:00, server local time — run the server in the properties'
  timezone). The scheduler releases holds when business hours begin; a manager
  can still approve a held request early. (Decided 2026-09-29.)

## AppFolio integration

- Phase 1: **read-only**. Import tenants, units, vendors (names, phones,
  trades). This is what lets inbound texts match to a tenant and unit.
- Two-way work-order push: later phase.

## Users and access

- Single manager login for now. Team roles later.

## Out of scope (for now)

- Voice/call handling and voicemail transcription.
- Vendor ETA promises to tenants (AI must not commit to arrival times).
- Any auto-send without human approval.

---

### Decision note: vendor reply parsing (item 3) — DECIDED: hybrid, 2026-09-29

- **Keyword match:** deterministic pattern list ("on my way", "omw",
  "confirmed", "yes" → accepted; "can't", "tomorrow", "busy", "no" →
  declined). Free, instant, no API dependency. Misses natural phrasing
  ("stuck on 540, be there in 40" has no keyword but clearly means yes).
- **AI parse:** the vendor's reply goes to the LLM: accepted / declined /
  unclear, plus ETA extraction. Handles typos, slang, other languages.
  Costs a few cents per message, adds ~1–2s, needs API key (falls back to
  keywords on failure).
- Recommended: hybrid — keywords first (fast path), AI only when keywords
  are inconclusive. Mirrors the existing rules-before-AI architecture.
