# Easol AI-Powered LBE Outbound Workflow

Structured around what the brief actually asks for: working outputs, the
workflow itself, and the thinking behind it. Full spec in [`PRD.md`](PRD.md).

---

## 1. Working outputs

5 outreach messages across 4 accounts (all 4 contact-role categories, 3
account categories) and 2 full briefings (one multi-site operator, one
zero-site edge case) — in this repo at
[`outputs/messages/`](outputs/messages/) and
[`outputs/briefings/`](outputs/briefings/), and walked through live in the
demo.
Click on [`outputs/dashboard.html`](outputs/dashboard.html) to see granular dashboard of outputs.

---

## 2. The workflow itself

### How I built it

Not written as a finished plan from day one — the process was:

1. **Pen and paper** — first pass at the workflow, the tools, and the open
   questions, before touching a keyboard.
2. **Collaborative planning with Claude** — took that starting point and
   worked through it properly: architecture, data model, prompting
   approach, and several rounds of deliberate critique to pressure-test
   scope and catch mistakes before they became build time.
3. **Settled on the end product** — a working thin-slice pipeline, not a
   full send-automation platform, with the scope boundaries and reasoning
   written down, not assumed.
4. **PRD** — every decision consolidated into one settled spec, treated as
   the source of truth for everything after this point.
5. **Cursor, plan mode** — the PRD went in as context, and Cursor produced
   a file-by-file implementation checklist against it.
6. **Build** — executed against that plan.
7. **Debug and iterate** — real gaps surfaced once code existed that
   planning hadn't caught (a schema field that was never actually wired up,
   an assumption about a third-party API that turned out wrong) — fixed as
   they came up, not treated as failures of the plan.

### How it runs

Three layers feed one shared SQLite context store, then two generation paths
read from it, then a human review gate decides what moves forward. The
important distinction throughout is which steps are deterministic, which
call external APIs without generating anything, and which actually call an
LLM — that's what keeps cost and risk where they belong.

**Deterministic, no API call, runs across all 120 accounts:** the CSV is
parsed into structured account and contact records; account-level
value-prop candidates are ranked from company facts (visits, category,
sites); those candidates are then weighted per contact based on their role.
All of this is plain Python in `load_data.py` and `context_builder.py`,
costs nothing, and runs on the full target list before anything expensive
happens — it's what lets the system prove it handles all 120 accounts
cheaply, even though only a handful get full treatment.

**External data, not generative:** Apollo is template-only, not wired into
the main flow. `find_contact_email.py` does a People Match lookup with a
strict verification gate — `verified` plus a domain match, both required
before anything lands in `contacts.resolved_email` — it's send-time
metadata and never enters LLM context. `apollo_push_campaign.py` pushes an
approved campaign's subject/bodies onto an Apollo contact's custom fields
for a shared sequence template; it does not enroll, activate, or send. Both
scripts are dry-run by default and deliberately decoupled from generation
and review — real template code proving the mechanism works, not a live
send path.

**Claude Sonnet 5 — three call sites, only one of which does web search:**

1. **Enrichment** (`enrich_account.py` → `research.py`) — the only place
   web search happens, via Anthropic's built-in tool (up to three searches
   per run). Cited facts land in `research_evidence`. Runs on demand for
   demo accounts only, not all 120.
2. **Campaign generation** (`generate_message.py`) — reads the latest
   completed enrichment dossier; no web search of its own. Produces a
   4-touch outreach campaign.
3. **Briefing generation** (`generate_briefing.py`) — reads the same
   dossier for that account; no web search of its own.

Research is shared, not duplicated — one dossier per account serves both
the campaign and the briefing. If it's stale before a call (generators
refuse anything older than seven days), you re-run `enrich_account.py` and
regenerate rather than researching inline inside the generators. Fresh
cited enrichment can override the deterministic value-prop default; the
final choice is logged in `drafts.value_prop_used` / `value_prop_reason`.
Governance — guardrails, the never-say list, every claim tracing back to
something in context — applies to both generation paths equally; campaigns
also get post-generation checks (banned phrases, per-touch word limits).
Every call to any of these three sites logs input tokens, output tokens,
and estimated cost against the prompt version that produced it — so unit
cost per message, per briefing, and per enrichment run is a real, queryable
number, not an estimate.

**The review gate, and what actually "sends":** `review.py` is the only
human gate in the built system. Nothing leaves `pending_review` without an
explicit choice — approve, edit, or reject. Rejecting requires picking a
reason from a numbered list of eight codes (generic, incorrect claim, weak
value case, wrong role angle, poor tone, too long, compliance risk, other);
approving or editing doesn't ask for one. Outreach campaigns are reviewed
per touch — all four shown together for narrative context, the original
`ai_*` content stays immutable, and edits land in `current_*` fields plus
`draft_events`. Briefings stay whole-draft.

What happens after approval is designed, not built live: an approved
campaign can optionally be pushed to Apollo's custom fields via
`apollo_push_campaign.py`, but enrollment and sending stay in the Apollo UI
or MCP — there's no poller, and it's never called automatically from
`review.py`. An approved briefing has no live email dispatch in this repo;
delivery is documented in
[`design/send_sequence_design.md`](design/send_sequence_design.md)
(Apollo + transactional email), explicitly out of scope because a live send
flow wouldn't produce evidence toward the six pilot risk questions.

Actually sending to a real person, LLM generation across all 120 accounts,
and individual-level web research on synthetic contacts are all explicitly
excluded. This build targets ~5 four-touch campaigns and ~2 briefings as
the evidence bar, not superficial coverage of everything.

### What tools I used, and why

- **Cursor** — for the actual build. Agentic, file-based editing fit a
  multi-file repo with a real schema better than a notebook would have.
- **Claude** — in two distinct roles. First as a planning and architecture
  partner throughout the design process (the PRD, the schema, the critique
  rounds that caught real mistakes before they were built). Second, inside
  the shipped pipeline itself, as Claude Sonnet 5 doing the actual
  generation — chosen for strong instruction-following on tone and negative
  constraints, which matters more here than raw benchmark performance given
  how much of the prompt is guardrails.
- **My own Obsidian knowledge base** — notes built up from prior work and
  from sales frameworks (Alex Hormozi's material among them) fed into how
  the outreach tone and structure were reasoned about, not just generated
  from a blank prompt.
- **Apollo** — for contact/email sourcing, and because it has an official
  MCP server, which raises a real build-vs-buy question for Easol
  specifically: Apollo could plausibly connect directly into Anvil rather
  than needing a custom integration layer built from scratch. Flagged as a
  decision worth making deliberately, not assumed.

---

## 3. Your thinking

### Key trade-offs

- **SQLite for the backend, never for anything human-facing.** SQLite and
  the human-facing delivery surface were always two separate decisions, not
  competing options. One keeps the data correct — transactions, an
  append-only audit trail (`draft_events`), validated state changes
  (`ALLOWED_TRANSITIONS`). The other is whatever reps and ops actually look
  at, built as a layer on top, and that always depended on Easol's real
  ecosystem — which I didn't know at build time. Now that it's confirmed as
  Google Workspace, the natural human-facing layer is a Google Sheet
  reading from the same SQLite backend; `dashboard.html` in this build is
  the stand-in for that.
- **A shared research dossier, not two fully independent research calls.**
  The original design ran separate research for every message and every
  briefing, on the reasoning that a message and a briefing have different
  freshness needs. Once generation became rep-triggered rather than
  pipeline-triggered, one shared mechanism with a freshness policy per
  command (tight for briefings, looser for messages) did the same job with
  less redundant research spend.
- **Apollo over a custom script or Resend.** Apollo's own sequence engine
  already does send, wait, reply-detection, and auto-stop-on-reply
  natively. Building that by hand — Resend for sending, custom logic for
  the wait/reply/cancel mechanics — would have reimplemented something a
  purpose-built tool already handles reliably. Traded some direct control
  over the exact mechanics for real time saved and a verified integration
  pattern (custom dynamic variables) rather than one built from scratch and
  untested.

### If I had more time

- **Prompt engineering itself** — the prompts work, but they haven't been
  iterated against real output variance the way something shipping to reps
  would need. More time here specifically, not just on the surrounding
  system.
- **A proper build-vs-buy pass on sales engagement tooling, done in two
  separate steps** — first, whether Apollo is actually the right choice at
  all against other sales engagement tools or a custom build, which I
  haven't validated, just defaulted to. Second, and only once a tool is
  actually chosen: what its MCP support specifically enables and which of
  those use cases genuinely fit Easol, rather than treating "it has an
  MCP server" as justification on its own.
- **The actual end-user experience, and not just Slack or Anvil** — those
  were working assumptions during the build, not confirmed. I've since
  learned Easol runs on Google Workspace, which answers the broad ecosystem
  question but not the specific one — whether that means a live Sheet, a
  Doc, or something else, and exactly where this should show up in a rep's
  day, still needs learning directly from how reps and ops actually work,
  not assumed from the platform alone.
- **The depth of the feedback loop, not just its presence** — what's built
  captures a lightweight, structured signal per draft (approve/edit/reject,
  a reason code on reject, an edit diff). That's real, but it's a thin
  proxy for whether reps actually trust the output. With more time I'd want
  a genuinely qualitative layer alongside it — actual conversations with
  reps using it, not just aggregated signals — before treating what's
  captured today as sufficient on its own.
- **Cost tracking at the aggregate level** — per-asset logging is already
  built and described above. What's missing is the layer on top of it: a
  real aggregate view, a budget, alerting on a regression — the kind of
  thing that catches a cost problem before it's already happened rather
  than after.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
```

`data/lbe_target_accounts.csv` and `PRD.md` are already in this repo.

## Run order

All commands run from the repo root, as `python src/foo.py` — see the import
strategy note in PRD §18 (bare sibling imports, no `-m`, no `__init__.py`).

```bash
# 1. Data & context — no API calls, all 120 accounts
python src/load_data.py
python src/context_builder.py

# 2. Enrichment — one company dossier per account, no generation yet
python src/enrich_account.py --all-demo

# 3. Task A — 5 four-touch outreach campaigns, reads the enrichment dossier, no web search
python src/generate_message.py

# 4. Task C — 2 briefings, reuses the same dossier per account, no web search
python src/generate_briefing.py

# optional: pick one account/contact instead of the full demo sample
python src/enrich_account.py --account "Hollywood Bowl"
python src/generate_message.py --account "Puttshack" --contact "Zoe Young"
python src/generate_briefing.py --account "Hollywood Bowl"
python src/generate_briefing.py --force --account "Hollywood Bowl"

# 5. Review — approve/edit/reject for campaigns, per touch (briefings stay whole-draft)
python src/review.py

# 6. Presentation
python src/dashboard.py
# open outputs/dashboard.html
```