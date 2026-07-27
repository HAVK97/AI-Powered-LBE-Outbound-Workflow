# Apollo campaign push — template design (not auto-initiated)

Stage 8 prep. This repo **pushes approved campaign content** onto an Apollo
contact; it does **not** enroll, activate, or send. Enrollment and send live in
the Apollo platform (or an Apollo MCP), outside this codebase.

## Why this split

- Review + content quality evidence stays in SQLite (`draft_touches`,
  `draft_events`).
- Delivery mechanics (scheduling, reply-stop, mailbox warm-up) stay in Apollo.
- No poller here calls `add_contact_ids` — nothing in `review.py` or a cron
  starts a sequence. That avoids silently emailing synthetic contacts.

## Content shape (already produced by `generate_message.py`)

```json
{
  "value_prop_used": "...",
  "value_prop_reason": "...",
  "touches": [
    {"step": 1, "delay_days": 0, "channel": "email", "subject": "...", "body": "..."},
    {"step": 2, "delay_days": 4, "channel": "email", "subject": "...", "body": "..."},
    {"step": 3, "delay_days": 7, "channel": "email", "subject": "...", "body": "..."},
    {"step": 4, "delay_days": 14, "channel": "email", "subject": "...", "body": "..."}
  ]
}
```

Per-touch review updates `draft_touches.current_subject` / `current_body`.
`queue.resolve_campaign_content()` is the single reader for push.

## Apollo pattern: one shared sequence + 5 custom fields

Apollo Sequences are template + merge fields. Bespoke AI bodies go into
**contact custom fields** (multi-line text), which the sequence steps reference
as dynamic variables. Documented Apollo pattern for AI-generated email bodies.

| Custom field | Used in |
|---|---|
| `touch_1_subject` | Sequence step 1 subject |
| `touch_1_body` | Sequence step 1 body |
| `touch_2_body` | Sequence step 2 body |
| `touch_3_body` | Sequence step 3 body |
| `touch_4_body` | Sequence step 4 body |

Steps 2–4: enable Apollo's **"Reply to previous thread"** so they inherit the
thread/subject from touch 1 (no subject custom fields for 2–4).

Delays in the shared sequence: day 0 / +4 / +7 / +14 (match `delay_days`).

## One-time Apollo UI setup

1. Create 5 contact custom fields (multi-line for bodies; subject can be
   single- or multi-line).
2. Note each field's ID (Fields API / Get a List of All Custom Fields).
3. Put IDs in `.env` as `APOLLO_FIELD_TOUCH_*`.
4. Build one Sequence with 4 auto-email steps; body of each step = only the
   corresponding custom dynamic variable; step 1 subject = `touch_1_subject`
   variable; steps 2–4 reply-to-thread.
5. Store sequence id in `APOLLO_SEQUENCE_ID` (reference only — this repo does
   not call enroll).
6. Connect a sending mailbox in Apollo (needed when *you* enroll later).

## Runtime flow in this repo

```
generate_message.py  → drafts + draft_touches (pending_review)
review.py            → per-touch approve / edit / reject → rollup drafts.status
apollo_push_campaign.py --draft-id N [--live]
                     → Create/Update Apollo contact + PATCH typed_custom_fields
                     → audit row in apollo_push_runs
                     → STOP (no enroll / no send)
```

Then in Apollo UI or MCP: add that contact to `APOLLO_SEQUENCE_ID`.

## Governance checks at push (fail closed)

- All touches approved / approved_with_edits (`campaign_ready_to_push`)
- `drafts.status` approved / approved_with_edits
- `contacts.resolved_email` present
- Not in `suppressions`
- Master API key for contact create/update (Apollo privilege)

## What this deliberately does not do

- `emailer_campaigns/.../add_contact_ids` (enroll)
- Sequence activate / send
- Webhook ingest for opens/replies (later)
- Auto-poll after review
