-- AI Outbound schema (PRD §4)
-- Note: accounts.researched_at intentionally omitted. enrichment_runs is the
-- source of truth for enrichment timing/status; research_evidence.retrieved_at
-- records when each individual claim was captured within its run.

CREATE TABLE IF NOT EXISTS accounts (
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
    vendor_annual_revenue REAL,             -- the vendor's own take — never Creator-facing
    candidate_value_props TEXT             -- JSON, ranked list from deterministic scoring
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(account_id),
    contact_name TEXT,
    contact_title TEXT,
    contact_category TEXT,                 -- Commercial | Finance | Operations | CEO / Managing Director (exact CSV string)
    role_default_value_props TEXT,         -- JSON, from ROLE_MAP
    resolved_email TEXT,                   -- only ever set by a 'verified' match — see contact_enrichment_runs
    resolved_email_run_id INTEGER REFERENCES contact_enrichment_runs(contact_enrichment_run_id)
);

-- one on-demand company dossier per run, shared by both outreach messages and
-- briefings for that account (no separate research "purposes" — see PRD §11).
-- Runs are triggered independently via src/enrich_account.py, never inline
-- inside generation. Append-only: a refresh adds a new run, it never wipes
-- an older one — generation always reads the latest 'completed' run.
CREATE TABLE IF NOT EXISTS enrichment_runs (
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
    error TEXT                             -- populated when status = 'failed'
);

CREATE INDEX IF NOT EXISTS idx_enrichment_runs_lookup
    ON enrichment_runs(account_id, status, completed_at);

-- structured, citable research — the source of truth for anything claimed to be current.
-- Each row belongs to one enrichment_run (see above). triggered_by is kept for
-- legacy rows written before this table existed ('message_draft' | 'briefing_prep');
-- all new rows use 'enrich'.
CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    triggered_by TEXT NOT NULL,            -- 'enrich' (new) | 'message_draft' | 'briefing_prep' (legacy)
    claim TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    enrichment_run_id INTEGER REFERENCES enrichment_runs(enrichment_run_id)
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_run
    ON research_evidence(account_id, enrichment_run_id);

-- Stage 3b — person-level email lookup (template, PRD §2/§11 caveat). Decoupled
-- from company enrichment_runs above: this is per-contact, not per-account, and
-- is NOT read by generate_message.py/generate_briefing.py — email address is
-- send-time metadata, never LLM context. Contacts in this dataset are almost
-- certainly synthetic, so a "successful" match is more likely a name collision
-- with a real stranger than a real hit (same misattribution risk PRD §2 already
-- flags for individual research generally). match_confidence is OUR gate, kept
-- separate from Apollo's own email_status — status='completed' here does NOT
-- imply a usable email. Only a 'verified' result is ever copied to
-- contacts.resolved_email; append-only, one row per lookup attempt.
CREATE TABLE IF NOT EXISTS contact_enrichment_runs (
    contact_enrichment_run_id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(contact_id),
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    status TEXT NOT NULL,                  -- 'running' | 'completed' | 'failed'
    provider TEXT NOT NULL DEFAULT 'apollo',
    request_params TEXT,                   -- JSON: {first_name, last_name, organization_name, domain}
    matched_person_id TEXT,                -- Apollo person id, if any match returned
    matched_name TEXT,                     -- name Apollo returned, for eyeballing mismatches
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

CREATE INDEX IF NOT EXISTS idx_contact_enrichment_lookup
    ON contact_enrichment_runs(contact_id, status, completed_at);

CREATE TABLE IF NOT EXISTS drafts (
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
    estimated_cost_usd REAL,
    status TEXT,                           -- pending_review | approved | approved_with_edits | rejected | queued | sent
                                           -- for outreach_campaign: rollup of draft_touches once all touches decided
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    enrichment_run_id INTEGER REFERENCES enrichment_runs(enrichment_run_id)  -- null for drafts predating this table
);

-- Per-touch review unit for outreach_campaign drafts. All 4 touches are shown
-- together for narrative context; decisions (approve / edit / reject) are
-- recorded per step. ai_* is the immutable original; current_* is what Apollo
-- push / send reads after edits. Never overwrite ai_*.
CREATE TABLE IF NOT EXISTS draft_touches (
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

CREATE INDEX IF NOT EXISTS idx_draft_touches_draft
    ON draft_touches(draft_id, status);

-- single source of truth for every review action. Append-only, never edited in place.
-- rating is mandatory on every event — see PRD §7.
-- draft_touch_id is NULL for whole-draft events (briefings, rollup, legacy);
-- set for per-touch campaign review events.
CREATE TABLE IF NOT EXISTS draft_events (
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

-- Template audit for Apollo content push (Stage 8 prep). This codebase pushes
-- approved campaign content onto an Apollo contact's custom fields; enrollment
-- / activation is done in Apollo UI or via MCP, not by a poller here.
CREATE TABLE IF NOT EXISTS apollo_push_runs (
    apollo_push_run_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(draft_id),
    contact_id INTEGER REFERENCES contacts(contact_id),
    status TEXT NOT NULL,                  -- 'dry_run' | 'pushed' | 'failed'
    apollo_contact_id TEXT,
    payload TEXT,                          -- JSON of custom fields + contact params sent/would-send
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS suppressions (
    suppression_id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(contact_id),
    account_id INTEGER REFERENCES accounts(account_id),
    reason TEXT NOT NULL,                  -- distinct from a rejected draft: this means "don't contact again"
    source TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
