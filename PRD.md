# PRD — Easol AI-Powered LBE Outbound Workflow
**Applied AI Lead case study — final spec**

Stack: Python (core logic) + SQLite (state/queue/audit) + Claude Sonnet 5 (generation) + Claude's built-in web search (research) + a CLI review tool + a static HTML export (room presentation) + Apollo template scripts (contact email lookup + campaign content push — dry-run by default, not a live send path). Send/sequencing is designed not built: Apollo is the preferred production path (`design/apollo_campaign_push.md`); n8n remains documented as an alternative (`design/send_sequence_design.md`, §7). Built in Cursor.

> **Note for implementation planning tools:** this is the final, settled spec after multiple rounds of critique and revision. Treat scope and schema as decided — plan file-level tasks and build order against this document, don't re-derive tradeoffs already resolved here. If something genuinely looks wrong, flag it as a question rather than silently changing it.

---

## 1. Overview

Easol's sales team manually researches, drafts, and preps outreach for 120 LBE target accounts. It's slow and inconsistent. This build is not a demo that an LLM can write an email — everyone already knows that. It's a pilot-ready thin slice of a real pipeline, built to produce evidence on six specific risky assumptions:

1. Can research/context be grounded and current, not generic?
2. Do outputs read as genuinely specific, not templated?
3. Does role + account signal produce the right value case for that person?
4. Can a salesperson review this fast enough for it to save time, not cost it?
5. Is the workflow safe, traceable, and auditable?
6. Do the unit economics make sense?

Every design decision below exists to produce evidence on one of these — not simply to produce output.

---

## 2. Scope

### In scope — build all of this
- Deterministic context + scoring pipeline, run across **all 120 accounts** (cheap, no API cost)
- **Research, core** — not a stretch goal — one shared company dossier per account via `enrich_account.py`, read by both Task A (campaign generation) and Task C (briefing generation); re-run on demand when stale (7-day freshness policy)
- **Evidence-informed value-prop selection** — the deterministic ranking is the candidate shortlist; at generation time the model is instructed to prefer whichever candidate the fresh research actually supports, and to say why
- Task A: **5 four-touch outreach campaigns** (one campaign per contact), across 4 accounts, spanning all 4 contact-role categories and 3 different account categories (top of the brief's stated 3-5 range, not the minimum)
- Task C: 2 full briefing notes — one multi-site operator, one zero-site touring business
- Task B: a working SQLite-backed queue with a real approve/edit/reject flow via a CLI (`review.py`) — **per-touch review for campaigns**, whole-draft review for briefings — including a **quality rating with a required reason on any negative rating** (see §7), plus documented send/sequence designs (Apollo preferred, n8n alternative) — not built live
- **Apollo template scripts** — `find_contact_email.py` (People Match with strict verification gate) and `apollo_push_campaign.py` (push approved campaign content onto Apollo custom fields). Dry-run by default; decoupled from generation and review; proves the send-prep mechanism without enrolling or sending
- Cost/token logging per generated asset
- A real feedback/audit trail (`draft_events`) — no fabricated data
- A **static, read-only HTML export** of the pipeline's output (accounts scored, drafts, statuses) for the room walkthrough
- Governance, adoption, and roadmap documentation

### Explicitly excluded — and why these are principled, not time-limited
- **Actually sending any message to a real person.** Not a build-time constraint — you don't send unsolicited outreach to real humans as part of a take-home exercise, regardless of how much time is available.
- **LLM generation across all 120 accounts.** Not a build-time constraint either — the brief explicitly says it would "rather see a focused, well-built solution across fewer areas than a superficial attempt at everything," and ~5 four-touch campaigns / 1-2 briefings is the stated evidence bar. Generating for all 120 would be scope creep against the brief's own words, not thoroughness.
- **Full legal review of outbound compliance.** Requires actual counsel — not something more engineering time changes.
- **An interactive multi-user web app beyond the CLI.** The CLI is a deliberate, legitimate engineering choice for a review tool — not a compromise made for lack of time. The static HTML export above covers the presentation need without pretending a browser-based approval flow adds real evidence of anything.
- **Individual-level web research for LLM context.** Contacts in this dataset are almost certainly synthetic names generated for the exercise. Searching for them risks either irrelevant results or misattributing a real stranger's information to a fictional contact — a real risk, not a time-saving shortcut. `individual_profile` stays openly role-inferred. **Exception:** `find_contact_email.py` does a template Apollo People Match for send-time metadata only — it never enters LLM context, requires `verified` + domain match before writing `contacts.resolved_email`, and is expected to produce mostly `no_match` rows against synthetic names.
- **Evidence-adjusted value-prop ranking as a weighted scoring engine** (role_affinity + account_signal + evidence_confidence formulas). The lightweight version — the model preferring a research-supported candidate over the deterministic default, see §10 — ships now. A full numeric scoring engine needs input signals (calibrated confidence scores, etc.) this dataset doesn't actually have; building one would be precision theater, not a real improvement.
- **A live send/sequence flow.** No poller, no auto-enrollment, no email dispatch from this repo. `apollo_push_campaign.py` pushes content onto Apollo custom fields only; enrollment and send stay in Apollo UI / MCP. A live n8n flow was also considered and reverted for the same reason — a live send flow doesn't produce evidence toward any of the six risk questions in §1. Both paths are documented: `design/apollo_campaign_push.md` (preferred) and `design/send_sequence_design.md` (n8n alternative).
- **Full production-grade send infrastructure** (Postgres-backed queue, idempotency keys, dead-letter queue, real status write-back from the send platform into the database). Documented in the design docs as what production needs. Not built — requires a persistent backend service to close the loop safely.

---

## 3. Users

- **Primary: Easol sales rep** — receives a draft outreach campaign to review (4 touches), and a briefing note ahead of a first call.
- **Secondary: Applied AI Lead / sales ops** — owns prompt quality, cost, and the review data used to iterate.

---

## 4. Data model & schema

Source: `lbe_target_accounts.csv` — 120 accounts, **250** contact rows (72 accounts have 2 contacts, 29 have 3, 19 have 1). No missing values.

**Verified relationships, confirmed against the actual data — build on these, don't re-derive:**
- `total_transaction_volume = annual_visits × avg_ticket_price` — exact, all 120 rows.
- `easol_annual_revenue = total_transaction_volume × 0.035` — exact flat rate, all 120 rows. This is Easol's own take — **never** shown in Creator-facing output.
- The CSV's `#` column is a stable 1-120 account index, identical across every contact row belonging to that account — verified with zero exceptions. Use it directly as `account_id`; no dedup-by-name logic needed.
- 6 accounts have `sites = 0` — Secret Cinema, Bongo's Bingo, Murder Mystery Co., Faulty Towers Dining, Funicular Productions, Cluedupp Games — touring/pop-up producers, not fixed venues. Branch on this.
- Contacts are tagged by role category: `Commercial`, `Finance`, `Operations`, `CEO / Managing Director` (keep this exact string — no normalization).

```sql
CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY,        -- = CSV '#' column, verified stable
    account_name TEXT NOT NULL,
    category TEXT,
    sub_category TEXT,
    description TEXT,
    hq_location TEXT,
    region TEXT,
    sites INTEGER,
    is_touring_business BOOLEAN,           -- derived: sites = 0
    annual_visits INTEGER,
    avg_ticket_price REAL,
    total_transaction_volume REAL,         -- derived: visits * price
    easol_annual_revenue REAL,             -- Easol's own take — never Creator-facing
    candidate_value_props TEXT             -- JSON, ranked list from deterministic scoring
);

CREATE TABLE contacts (
    contact_id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(account_id),
    contact_name TEXT,
    contact_title TEXT,
    contact_category TEXT,                 -- Commercial | Finance | Operations | CEO / Managing Director (exact CSV string)
    role_default_value_props TEXT,         -- JSON, from ROLE_MAP
    resolved_email TEXT,                   -- only ever set by a 'verified' match — see contact_enrichment_runs
    resolved_email_run_id INTEGER REFERENCES contact_enrichment_runs(contact_enrichment_run_id)
);

-- one on-demand company dossier per run, shared by both outreach campaigns and
-- briefings for that account — enrichment is decoupled from generation and
-- triggered independently via enrich_account.py. Append-only history; see §11.
CREATE TABLE enrichment_runs (
    enrichment_run_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    status TEXT NOT NULL,                  -- 'running' | 'completed' | 'failed'
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    web_search_requests INTEGER,
    estimated_cost_usd REAL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT
);

-- structured, citable research — the source of truth for anything claimed to be current.
-- Each row belongs to one enrichment_run; triggered_by='enrich' for all new rows
-- ('message_draft' | 'briefing_prep' only appear on rows predating this table).
CREATE TABLE research_evidence (
    evidence_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    triggered_by TEXT NOT NULL,            -- 'enrich' (new) | 'message_draft' | 'briefing_prep' (legacy)
    claim TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    enrichment_run_id INTEGER REFERENCES enrichment_runs(enrichment_run_id)
);

CREATE INDEX idx_research_evidence_run
    ON research_evidence(account_id, enrichment_run_id);

-- Stage 3b — person-level email lookup (template). Decoupled from company
-- enrichment: per-contact, NOT read by generators — email is send-time metadata,
-- never LLM context. Only a 'verified' result with domain match is copied to
-- contacts.resolved_email; append-only, one row per lookup attempt.
CREATE TABLE contact_enrichment_runs (
    contact_enrichment_run_id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(contact_id),
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    status TEXT NOT NULL,                  -- 'running' | 'completed' | 'failed'
    provider TEXT NOT NULL DEFAULT 'apollo',
    request_params TEXT,                   -- JSON: {first_name, last_name, organization_name, domain}
    matched_person_id TEXT,
    matched_name TEXT,
    matched_organization_name TEXT,
    matched_organization_domain TEXT,
    email TEXT,                            -- raw candidate email, pre-verification-gate
    email_status TEXT,                     -- Apollo's own field: verified | unverified | extrapolated | null
    match_confidence TEXT,                 -- 'verified' | 'unverified' | 'no_match' | 'org_mismatch' | 'dry_run'
    credits_used INTEGER,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT
);

CREATE TABLE drafts (
    draft_id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(account_id),
    contact_id INTEGER REFERENCES contacts(contact_id),   -- null for briefings
    draft_type TEXT,                       -- 'outreach_campaign' | 'outreach_message' (legacy) | 'briefing'
    content TEXT,                          -- immutable AI JSON for campaigns; briefings store rendered markdown
    value_prop_used TEXT,                  -- Task A only, parsed from output contract
    value_prop_reason TEXT,                -- Task A only: 'default' or a short note on which evidence overrode it
    prompt_version TEXT,
    model TEXT,
    input_context TEXT,                    -- JSON snapshot: account+contact context and research evidence used
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL,               -- generation only; enrichment cost lives on enrichment_runs
    status TEXT,                           -- pending_review | approved | approved_with_edits | rejected | queued | sent
                                           -- for outreach_campaign: rollup of draft_touches once all touches decided
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    enrichment_run_id INTEGER REFERENCES enrichment_runs(enrichment_run_id)  -- null for drafts predating this table
);

-- Per-touch review unit for outreach_campaign drafts. All 4 touches are shown
-- together for narrative context; decisions (approve / edit / reject) are
-- recorded per step. ai_* is the immutable original; current_* is what Apollo
-- push / send reads after edits. Never overwrite ai_*.
CREATE TABLE draft_touches (
    draft_touch_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(draft_id),
    step INTEGER NOT NULL,                 -- 1-4
    delay_days INTEGER NOT NULL,
    channel TEXT NOT NULL,
    ai_subject TEXT NOT NULL,
    ai_body TEXT NOT NULL,
    current_subject TEXT NOT NULL,
    current_body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    UNIQUE (draft_id, step)
);

-- single source of truth for every review action. Append-only, never edited in place.
-- rating is mandatory on every event now — see §7 for the full flow.
-- draft_touch_id is NULL for whole-draft events (briefings, rollup, legacy);
-- set for per-touch campaign review events.
CREATE TABLE draft_events (
    event_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(draft_id),
    draft_touch_id INTEGER REFERENCES draft_touches(draft_touch_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,                   -- 'self_review' for this build
    rating INTEGER NOT NULL,               -- 1 = thumbs up, 0 = thumbs down — captured on every event
    reason_code TEXT,                      -- required when rating = 0. generic | incorrect_claim | weak_value_case | wrong_role_angle | poor_tone | too_long | compliance_risk | other
    quality_note TEXT,                     -- optional free-text elaboration, only meaningful when rating = 0
    edited_content TEXT,                   -- populated when to_status = approved_with_edits (JSON for touches)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Template audit for Apollo content push (Stage 8 prep). Pushes approved
-- campaign content onto an Apollo contact's custom fields; enrollment /
-- activation is done in Apollo UI or via MCP, not by a poller here.
CREATE TABLE apollo_push_runs (
    apollo_push_run_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(draft_id),
    contact_id INTEGER REFERENCES contacts(contact_id),
    status TEXT NOT NULL,                  -- 'dry_run' | 'pushed' | 'failed'
    apollo_contact_id TEXT,
    payload TEXT,                          -- JSON of custom fields + contact params sent/would-send
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE suppressions (
    suppression_id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(contact_id),
    account_id INTEGER REFERENCES accounts(account_id),
    reason TEXT NOT NULL,                  -- distinct from a rejected draft: this means "don't contact again"
    source TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Every connection opens with `PRAGMA foreign_keys = ON` (off by default in SQLite). Every status change is validated against a fixed `ALLOWED_TRANSITIONS` map before being written — no status is ever edited directly, only through `draft_events`.

```python
ALLOWED_TRANSITIONS = {
    "pending_review": {"approved", "approved_with_edits", "rejected"},
    "approved": {"queued"},
    "approved_with_edits": {"queued"},
    "queued": {"sent"},
    "rejected": set(),
    "sent": set(),
}
```

---

## 5. Workflow — end to end

**Stage 1 — ingest.** CSV loads into `accounts` and `contacts`. `account_id` = the CSV's own `#` column. *Python + SQLite.*

**Stage 2 — deterministic scoring, all 120 accounts, no API cost.** Compute `total_transaction_volume`, `is_touring_business`, and `candidate_value_props` (rule-based, §10) for every account; `role_default_value_props` for every contact. Pure arithmetic and lookups — proves the pipeline handles the full target list before anything expensive runs. *Python only.*

**Stage 3 — enrichment, on demand, decoupled from generation.** `enrich_account.py` produces one company dossier per account, run explicitly by the operator (`--account` or `--all-demo`), never triggered inline by a generator. Every invocation always performs web search and records a new `enrichment_runs` row (`running` → `completed`/`failed`); older runs are never overwritten — full history stays queryable. The same dossier is shared by that account's outreach campaign(s) and briefing alike: there is no separate "message" vs. "briefing" research pass. Freshness is enforced by `get_latest_enrichment()` — generators refuse dossiers older than 7 days (`DEFAULT_MAX_AGE_HOURS = 168`) and fail fast with the exact re-enrich command. *Tool: Claude's built-in server-side web search, called directly in the API request (§11) — not a separate search API.*

**Stage 3b — contact email lookup (template, optional).** `find_contact_email.py` does an Apollo People Match for send-time metadata only. Decoupled from company enrichment and from all generators — `resolved_email` never enters LLM context. Dry-run by default; `--live` costs Apollo credits. Only a `verified` match with domain match is copied to `contacts.resolved_email`. Expected to produce mostly `no_match` against synthetic contacts — that's correct, not a bug.

**Stage 4 — context assembly.** For the specific account (and contact, for Task A) about to be generated, Stage 2's deterministic facts and the account's latest completed `enrichment_runs` evidence (Stage 3) get pulled into one object — this is the actual thing handed to the model. For Task A, `role_weighted_value_props` (role defaults intersected with account candidates) is computed here alongside the three related prop lists (§10). Stage 5 doesn't do its own lookups or research — it only ever sees what's assembled here; if no completed enrichment run exists (or the latest one is stale), assembly fails fast with the exact `enrich_account.py` command to fix it.

**Stage 5 — generation, no web search.** The assembled context goes to the model. Task A produces one **4-touch email campaign** (JSON) per contact — not a single message. Task A also receives contact context (role only — never researched, §2). Task C also receives every contact on the account and pre-computed value-case figures — the model narrates these, never calculates them (§8). The model is instructed to prefer a research-supported candidate over the raw deterministic default, and to say why (§10). Generation cost/tokens are logged separately from enrichment cost/tokens (`drafts` vs. `enrichment_runs`) since this stage makes no search calls of its own. *Tool: Claude Sonnet 5.*

**Stage 6 — guardrail check, twice.** Once as an instruction in the prompt (tone, never-say list, grounding rule, CTA ladder). Once as a deterministic code check (banned phrases, per-touch word counts) before the draft is written to the database. Campaign word-count tolerance is wider than prompt targets and varies by touch (see §6). **On a violation:** print it to console at generation time and continue — don't block the insert. The draft still reaches `pending_review` either way; `review.py` and a human are the real gate regardless.

**Stage 7 — review.** Every draft starts `pending_review`. Outreach **campaigns** are reviewed **per touch** — all 4 touches shown together for narrative context, decisions recorded on `draft_touches`; `drafts.status` rolls up once every touch has a decision. **Briefings** keep whole-draft review. A human works through `review.py`: approve, approve-with-edits, or reject. Every action captures a quality rating and, if negative, a required reason (§7). Edits update `current_subject` / `current_body` on the touch row; `ai_*` fields stay immutable. Nothing moves further without a human decision — there is no automatic path to `sent` anywhere in this system.

**Stage 8 — send & sequence.** Not built live. Two documented paths:
- **Apollo (preferred):** `design/apollo_campaign_push.md` + `apollo_push_campaign.py` — after all touches are approved, optionally push campaign content onto an Apollo contact's custom fields for a shared sequence template. Enrollment and send stay in Apollo UI / MCP; this repo never calls `add_contact_ids` or dispatches email.
- **n8n (alternative):** `design/send_sequence_design.md` — poll on `status = approved`, wait ~4 days, check for a reply, send a follow-up if none, update status.

**Stage 9 — presentation.** A static HTML export renders the SQLite tables (scored accounts, drafts and their statuses, cost totals) as one read-only page for the room walkthrough. No interactivity, no new functionality — just a better-looking view of data that already exists.

---

## 6. Task A — outreach campaign generator

**Inputs:** account context (facts + ranked candidate value props with descriptions + this session's research) + contact context (name, title, role category — never researched) + three related prop lists: `role_weighted_value_props` (default shortlist), `candidate_value_props_ranked`, `role_default_value_props`.

**Output:** one **4-touch email campaign** per contact, stored as a single `outreach_campaign` draft with 4 `draft_touches` rows.

| Touch | Role | delay_days | Body length (prompt target) |
|---|---|---|---|
| 1 | Cold open — hook + primary outcome + interest-check CTA | 0 | 80–130 words |
| 2 | Bump — short follow-up referencing touch 1 | 4 | 40–80 words |
| 3 | Escalation — cost of inaction / new evidence; low-friction asset offer | 7 | 60–100 words |
| 4 | Breakup — permission close, easy out | 14 | 40–60 words |

`delay_days` are days after touch 1 (absolute: 0, 4, 7, 14). All touches are email. Touch 1 gets a fresh subject; touches 2–4 use reply-style subjects (`Re: …`). CTA ladder is mandatory: touch 1 = interest check only (no calendar/demo); touch 2 = binary bump; touch 3 = specific low-friction asset tied to the primary prop; touch 4 = permission close.

**Guardrails, enforced twice (prompt + code):**
- Reference at least one specific, real detail about the business in touch 1 — never generic phrasing.
- Connect only to the value prop(s) provided — never invent one. Touch 1 anchors the primary prop.
- Never state a fact, statistic, or date about the target company that isn't in the provided context.
- Never mention or disparage competitors by name.
- Tone: concise, credible, cold outreach — not a pitch deck.
- Banned phrases (code-enforced): `revolutionize`, `game-changer`, `synergy`, `leverage our`. Post-check word-count tolerance is per-touch and wider than prompt targets (e.g. touch 1: 60–160 words).

**Output contract:** JSON with `value_prop_used`, `value_prop_reason`, and `touches` (array of exactly 4 objects: `step`, `delay_days`, `channel`, `subject`, `body`). Parsed into `drafts.value_prop_used` / `value_prop_reason` and `draft_touches` — not left buried in free text. Prompt version: `campaign_v2` (`src/prompts/campaign_prompt.md`).

**Demo sample (5 campaigns, 4 accounts, all 4 roles, 3 categories):**
| Account | Category | Contact | Role |
|---|---|---|---|
| Puttshack | Competitive Socialising | Zoe Young, Head of Sales | Commercial |
| Puttshack | Competitive Socialising | Sian Gibson, Head of Operations | Operations |
| NQ64 | Competitive Socialising | Aisha Hall, Co-Founder & CEO | CEO / Managing Director |
| Secret Cinema | Immersive Theatre / Dining | Adam Grant, Head of Finance | Finance |
| Escape Reality | Escape Rooms | Katie Jackson, Head of Revenue | Commercial |

Puttshack's two contacts demonstrate account-context reuse (same dossier, different role-weighted props). Escape Reality's Commercial contact, alongside Puttshack's, demonstrates that the same role produces meaningfully different campaigns across different business types. Secret Cinema is deliberately shared with Task C's sample (§8) so the room walkthrough can show one account getting both a campaign and a briefing from the same shared dossier.

---

## 7. Task B — queue, review, and send

**Statuses:** `pending_review → approved / approved_with_edits / rejected → queued → sent`

**Review flow (`review.py`), including the quality rating mechanism:**

For **outreach campaigns**, all 4 touches are displayed together for narrative context, but approve/edit/reject decisions are recorded **per touch** on `draft_touches`. Once every touch has a decision, `queue._rollup_campaign_status()` sets `drafts.status` to the aggregate outcome (any rejection → draft rejected; all approved → approved; mix of approved/approved_with_edits → approved_with_edits). For **briefings**, the whole draft is reviewed in one step.

1. List pending drafts; show one (campaign touches or briefing content).
2. Human chooses per touch (campaigns) or per draft (briefings): approve / approve with edits / reject.
3. **If approve or approve-with-edits:** ask a quality question — thumbs up or down (`y`/`n`). If `y`, `rating = 1`, done. If `n`, `rating = 0` and the reviewer **must** select a `reason_code` from the controlled list, with an optional free-text `quality_note` for elaboration — both saved against the same `draft_events` row as the status change itself (with `draft_touch_id` set for campaign touches), not a separate record. This is deliberately allowed on an *approved* draft: a rep might approve something after editing it but still want to flag that it needed real fixing, not just polish — that's exactly the signal a production version would use to iterate on the prompt.
4. **If reject:** `rating` is auto-set to `0` and `reason_code` is required, same controlled list — no separate quality question.
5. **Edits on campaigns:** update `draft_touches.current_subject` / `current_body`; `ai_*` fields stay immutable. `queue.resolve_campaign_content()` is the single reader for Apollo push.
6. Suppression: if `reason_code = compliance_risk`, the contact is added to `suppressions` automatically.

**Reason codes (controlled, not free text — keeps the data aggregable):** `generic`, `incorrect_claim`, `weak_value_case`, `wrong_role_angle`, `poor_tone`, `too_long`, `compliance_risk`, `other`.

**Guardrails:** nothing leaves `pending_review` without a human action; a documented rate limit (20 sends/day/rep — a stated assumption, not a precise figure); every message needs an opt-out/objection path in production; suppressed contacts never re-enter the queue. `review.py` does **not** trigger Apollo enrollment or send.

**What's built vs. designed for send/sequence:**
- **Built, live:** the SQLite queue, `draft_touches`, status transitions, and the full review flow (§7 above) via `review.py`.
- **Built, template (dry-run default):** `find_contact_email.py` (Apollo People Match → `contacts.resolved_email`) and `apollo_push_campaign.py` (push approved campaign content onto Apollo custom fields → `apollo_push_runs`). Neither enrolls, activates, or sends.
- **Designed, not built:** send + sequencing mechanics. `design/apollo_campaign_push.md` (preferred — Apollo sequence engine owns wait/reply-stop) and `design/send_sequence_design.md` (n8n alternative — transactional email path). Production needs a Postgres-backed queue with idempotency keys and a dead-letter queue to avoid duplicate sends.

**Feedback capture — the real flywheel:** every review event now carries outcome, quality rating, and (when negative) a structured reason plus optional detail. This is real, available-on-day-one signal — not fabricated reply data — and it's exactly what a production version would use to iterate on the prompt.

**Room narrative:** walk one record through the whole system — CSV row → account context → enrichment dossier → generated 4-touch campaign → per-touch review with a genuine thumbs-down-and-reason on at least one touch → the resulting `draft_events` row → the Apollo push design for what happens next. Then show Secret Cinema getting both a campaign and a briefing from the **same shared dossier**. A caught, explained mistake demonstrates more control than six clean outputs.

---

## 8. Task C — briefing note generator

**Sections:** company overview · individual profile (all contacts on the account — role-inferred, never researched) · value case (Python volume/uplift + model prop selection) · talking points. Competitive context is deferred — no competitor/stack DB yet.

**Non-negotiable rule:** the model narrates, Python calculates. Value-case figures are computed deterministically and passed in for narration only.

```
current_transaction_volume = annual_visits × avg_ticket_price     [exact, from data]
illustrative_conversion_uplift = current_transaction_volume × 25% [illustrative — see caveat]
illustrative_transaction_value_uplift = current_transaction_volume × 8%
```

**Caveat, hardcoded in Python, never model-generated (so its wording never depends on a given run):** *"These figures follow the brief's own illustrative method (visits × ticket price × uplift %). A precise ROI model would need current online conversion rate, traffic, and booking-vs-footfall breakdown, which aren't in this dataset — worth asking about live."*

**Currency note, also hardcoded:** figures are in $, matching the source CSV — the brief's own worked example uses £, a known inconsistency in the provided materials, flagged rather than silently converted.

**`easol_annual_revenue` never appears in Creator-facing output** — it's Easol's own take, not the Creator's upside. Comment this explicitly in code, not just in this doc; it's an easy swap to make by accident.

**Output contract:** JSON with exactly `company_overview`, `individual_profile`, `value_props_to_lead_with` (1–2 `{name, why}`), `talking_points` (3–4) — volume/uplift figures and caveats are rendered in Python, not left to the model. Prompt version: `briefing_v3` (`src/prompts/briefing_prompt.md`).

**Demo sample:** Hollywood Bowl (72 sites, 14M annual visits, $140M transaction volume — the largest, most dramatic value case in the dataset) and Secret Cinema (zero-site touring business — the edge case, and shared with Task A for the one-record room narrative).

---

## 9. Value proposition reference (verbatim — feed this, not just the name, into every prompt)

Without the actual descriptions, the model has nothing to work with except a label, and will genericize or invent what e.g. "AI Sales Agent" means. This is a fixed constant, not account-specific data:

```python
VALUE_PROPS = {
    "Conversion Rate": "Increases conversion rate by 25% through a smoother booking journey and wider payment options (e.g. group split pay, payment plans).",
    "Transaction Value": "Increases average transaction value by 8% through upsell and package options at point of booking.",
    "AI Customer Support": "Agentic AI for Customer Support allows the Creator to run the function at scale without adding team cost, whilst improving CSAT.",
    "AI Sales Agent": "Agentic AI for complex group and corporate sales enquiries — managing these end-to-end, freeing up team time, and increasing win rate.",
    "Cost & Complexity Reduction": "Reduces cost by removing the need for multiple different systems (e.g. separate systems for online booking, walk-ins and group sales). Also reduces operational complexity, freeing up time.",
}
```

`context_builder.py` defines this; both generators import it. The `<relevant_value_props>` block in every prompt includes the description, not just the name.

---

## 10. Role → value-prop mapping, and evidence-informed refinement

**Deterministic baseline (`ROLE_MAP`)** — the candidate shortlist, computed cheaply for all 120 accounts:

| Contact category | Default value props |
|---|---|
| Commercial | Conversion Rate, Transaction Value, AI Sales Agent |
| Finance | Cost & Complexity Reduction, Transaction Value (margin framing) |
| Operations | AI Customer Support, Cost & Complexity Reduction |
| CEO / Managing Director | Top 2 entries from that account's already-computed `candidate_value_props` (from `rank_value_props`) — not all five undifferentiated. No separate $-ranking step: Conversion Rate and Transaction Value are always first in that list by construction, so this resolves to those two without inventing dollar formulas for props that don't have them. |

**Account-level candidate ranking (`rank_value_props`)**, also deterministic, also cheap:
```python
def rank_value_props(account: dict, median_visits: float) -> list[str]:
    props = ["Conversion Rate", "Transaction Value"]  # always relevant
    if account["annual_visits"] > median_visits:       # ~375,000, computed at runtime across all 120
        props.append("AI Customer Support")
    if account["category"] in {"Competitive Socialising", "Immersive Theatre / Dining", "Karaoke"}:
        props.append("AI Sales Agent")
    if account["sites"] >= 3 or account["is_touring_business"]:
        props.append("Cost & Complexity Reduction")
    return props
```

**Role-weighted shortlist (`role_weighted_props`)**, computed at context-assembly time for Task A:
```python
def role_weighted_props(role_defaults: list[str], candidates: list[str]) -> list[str]:
    role_first = [p for p in role_defaults if p in candidates]
    rest = [p for p in candidates if p not in role_first]
    return role_first + rest
```
The first entry of `role_weighted_value_props` is the deterministic default for the campaign primary prop before evidence-informed override.

**Evidence-informed refinement, at generation time only (no new scoring code):** the role-weighted list is never the final answer by itself — it's superseded at generation time, once that account's research has actually run. The generation prompt receives `role_weighted_value_props`, `candidate_value_props_ranked`, `role_default_value_props`, *and* whatever fresh research evidence exists for that account, and only then does the real selection happen. It's instructed: if the research reveals something that makes one candidate clearly more relevant than the role-weighted default (e.g. evidence of active corporate/group bookings → weight AI Sales Agent higher), prefer that one (`value_prop_used`) and say why (`value_prop_reason`: `default` or a short note naming the overriding evidence). Every choice is traceable to either the default rule or a specific piece of evidence.

---

## 11. Research mechanism

```python
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 3,
    "allowed_callers": ["direct"],  # preserves citation objects on Sonnet 5
}
```

Passed directly in the Claude API call — Anthropic's built-in server-side web search, not a separate search API or key. Claude decides when to search, runs the query, returns cited results. Cost: $10 per 1,000 searches plus standard token cost for retrieved content — a few cents at this volume. `max_uses` caps runaway searching.

**One dossier per account, run on demand via `enrich_account.py`** — see §5, Stage 3, for the reasoning. Each call always performs web search, inserts a new `enrichment_runs` row (`running` → `completed`/`failed`, with its own token/search/cost accounting), and writes that run's cited claims to `research_evidence` tagged with `triggered_by='enrich'` and the owning `enrichment_run_id`. Generators never call this themselves; they read the latest `completed` run via `get_latest_enrichment()`. Runs are append-only — re-enriching adds a new row rather than overwriting the old one, so cost and history stay fully auditable.

**Freshness policy:** `DEFAULT_MAX_AGE_HOURS = 168` (7 days). Generators refuse stale dossiers and fail fast with the exact re-enrich command.

**Individual-level research for LLM context is deliberately excluded.** See §2 for the reasoning (synthetic contact names, misattribution risk). `individual_profile` in the briefing stays openly role-inferred. Apollo email lookup (`find_contact_email.py`) is send-time metadata only — never LLM context.

---

## 12. Model & prompting approach

**Model:** `claude-sonnet-5` via the Anthropic API. $2/$10 per million input/output tokens (introductory, through Aug 31 2026; standard $3/$15 after — update the pricing constant then). Chosen for strong instruction-following on tone and negative constraints, and reliable structured-context parsing — named and justified rather than assumed, since the brief specifically grades tool selection.

**Pattern:** a versioned system prompt (rules, tone, output contract) + a structured, XML-tagged context block per generation + a fixed, parseable output contract (JSON via structured outputs). Prompts live as files in `src/prompts/`, matched to `drafts.prompt_version` by a manual version string (`campaign_v2`, `briefing_v3`, `enrich_v1`), incremented by hand on edits. Legacy single-message prompt archived at `src/prompts/archive/message_prompt.md`.

**Shared plumbing:** `db.py` centralises connection factory (`PRAGMA foreign_keys = ON`), path constants, idempotent column migration, and batch error handling — every CLI script imports from it rather than duplicating boilerplate.

**Parameters:** temperature ~0.4-0.6 — grounded and controlled over creative.

---

## 13. Non-functional requirements

- **Cost logging:** every draft stores `input_tokens`, `output_tokens`, `estimated_cost_usd`, `model`; enrichment runs store the same on `enrichment_runs` — unit cost is measurable per asset type; total cost is demand-triggered on the demo sample, not a claim that cost "stays flat" at scale.
- **Auditability:** every draft traceable to its prompt version, its full `input_context` snapshot, and its complete `draft_events` history.
- **Data integrity:** foreign keys enforced, every status transition validated against `ALLOWED_TRANSITIONS` before being written.

---

## 14. Governance & compliance

- **Data:** minimise PII sent to the LLM API; a production rollout needs a DPA with the provider.
- **Content risk:** never-say list in the prompt, human approval before any send, logged prompt versions and full review history for traceability.
- **Compliance:** UK/EU cold outreach needs subscriber-type classification, a documented lawful basis, clear sender identification, an opt-out route, and suppression-list screening — presented as a requirement to confirm with counsel, not a legal opinion.
- **Cost:** per-run budget cap, generation demand-triggered on selected accounts only, never automatic across the full list.

---

## 15. Adoption plan

- Deliver into wherever the sales team already works (Slack/CRM/email) — an assumption to validate with real users, not guessed here.
- Approval UX stays trivially easy — a button/checkbox, not a new tool to learn.
- Track review outcome (rating, reason codes) as the real trust/quality signal.
- Pilot with 1-2 friendly reps before any wider rollout.

---

## 16. Success criteria

- [ ] Deterministic context builder runs cleanly across all 120 accounts, no errors.
- [ ] 5 four-touch outreach campaigns generated — all 4 contact roles, 3 account categories represented.
- [ ] 2 full briefings — one multi-site operator, one zero-site touring business.
- [ ] Every generated asset has logged cost, tokens, and prompt version.
- [ ] Every `draft_events` row has a rating; at least one genuine thumbs-down with a real reason code and note, not fabricated.
- [ ] Foreign keys enforced; transitions validated against `ALLOWED_TRANSITIONS`.
- [ ] Per-touch campaign review works via `draft_touches`; rollup status on `drafts` once all touches decided.
- [ ] Static HTML export renders correctly from the current database state.
- [ ] README states plainly that self-review demonstrates the mechanism works, not that reps trust the output.

---

## 17. Roadmap

**Phase 0 (this build):** everything above — full 120-account deterministic pass, evidence-informed generation on the demo sample, Apollo template scripts (dry-run), send/sequence designed not built, manual review only.
**Phase 1 — pilot (weeks 1-4):** real run on a small batch with 1-2 friendly reps, still human-sent, validate the delivery-surface assumption, confirm the DPA.
**Phase 2 — scale (months 2-3):** full 120-account generation, Apollo (or n8n) built and owns sequencing with status write-back into a production database, reply-rate data starts informing prompt iteration, a real weighted evidence-scoring engine if the inputs justify it, formal prompt ownership.
**Phase 3 — production (quarter+):** Postgres-backed queue with idempotency and DLQ, CRM-native delivery, adoption dashboard, pattern extended to other verticals/teams at Easol.

---

## 18. Repo structure

```
easol-ai-outbound/
├── PRD.md
├── README.md
├── requirements.txt
├── .env.example                    # ANTHROPIC_API_KEY + Apollo template keys
├── data/
│   └── lbe_target_accounts.csv
├── db/
│   └── easol.db
├── src/
│   ├── schema.sql
│   ├── db.py                       # shared connection factory, paths, migrations, batch runner
│   ├── load_data.py                # CSV -> accounts/contacts
│   ├── context_builder.py          # VALUE_PROPS, ROLE_MAP, rank_value_props, role_weighted_props, deterministic pass on all 120
│   ├── research.py                 # fetch_company_dossier / create_enrichment_run / get_latest_enrichment + ensure_schema
│   ├── enrich_account.py           # Stage 3 CLI — on-demand enrichment, decoupled from generation
│   ├── find_contact_email.py       # Stage 3b — Apollo People Match (template, dry-run default)
│   ├── generate_message.py         # Task A — 4-touch outreach campaigns
│   ├── generate_briefing.py        # Task C
│   ├── queue.py                    # ALLOWED_TRANSITIONS, draft_touches, draft_events, suppressions, Apollo push helpers
│   ├── review.py                   # CLI: per-touch (campaigns) / whole-draft (briefings) approve/edit/reject + quality rating
│   ├── apollo_push_campaign.py     # Stage 8 prep — push approved campaign to Apollo custom fields (template)
│   ├── dashboard.py                # renders the static HTML export
│   └── prompts/
│       ├── campaign_prompt.md      # Task A (campaign_v2)
│       ├── briefing_prompt.md        # Task C (briefing_v3)
│       └── archive/
│           └── message_prompt.md   # legacy single-message prompt
├── design/
│   ├── apollo_campaign_push.md     # Apollo send prep: custom fields + shared sequence template
│   └── send_sequence_design.md     # n8n alternative: trigger, wait, follow-up, tracking — not built live
├── outputs/
│   ├── messages/
│   ├── briefings/
│   └── dashboard.html
```

**Import strategy:** plain `python src/foo.py` from repo root, bare sibling imports (`from context_builder import VALUE_PROPS, ROLE_MAP`; `from db import get_connection`). Python auto-adds a directly-run script's own directory to `sys.path`, so this works with zero package structure — no `-m`, no `__init__.py`. Note: `src/queue.py` shadows the stdlib `queue` module when running scripts this way — HTTP calls use stdlib `urllib`, not `requests`, to avoid that collision.

---

## 19. Build order (sequence, not a schedule)

Cursor builds fast enough that hour-boxing this adds false precision rather than useful structure. What matters is dependency order:

1. **Data & context** — schema, `db.py`, load, deterministic context builder (all 120 accounts), pick and freeze the demo sample.
2. **Research** — `research.py`, `enrich_account.py`; generators read dossier via `get_latest_enrichment()` (never inline search).
3. **Task A** — `campaign_prompt.md`, generator, 5 four-touch campaigns + `draft_touches`.
4. **Task C** — `briefing_prompt.md`, generator, 2 briefings.
5. **Task B** — `queue.py` (per-touch transitions + rollup), `review.py` with the full rating/reason flow; design docs for Apollo + n8n send paths.
6. **Send prep (template)** — `find_contact_email.py`, `apollo_push_campaign.py` (dry-run default).
7. **Presentation & docs** — `dashboard.py`, README.

Later stages depend on earlier ones (context builder before generation, generation before review) — build in this order, but don't pace it against a clock.

---

## 20. Open assumptions to state explicitly in the room

- Delivery surface for reps (Slack/CRM/email) is assumed, not confirmed with Easol's actual sales team.
- Currency: $ throughout, matching the source CSV, not the brief's £ example.
- "Annual visits" is treated as a proxy for booking volume, not raw web traffic — the conversion-rate uplift figure inherits this limitation.
- Competitive context is general-knowledge-based unless explicitly flagged as researched.