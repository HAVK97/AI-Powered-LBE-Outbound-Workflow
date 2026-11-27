# AI-Powered LBE Outbound Workflow

A thin-slice pipeline for B2B outbound to location-based entertainment (LBE) operators. It researches target accounts, drafts four-touch outreach campaigns and pre-call briefings, and sends every draft through a human review gate before anything can go out.

- **Cheap where it can be.** Account and contact scoring runs deterministically across all 120 accounts with no API calls. Only demo accounts get LLM research and generation.
- **Evidence-bound.** Web research is cited and shared between campaigns and briefings. Every claim must trace back to that context.
- **Human in the loop.** Nothing leaves `pending_review` without an approve, edit or reject decision, and rejections need a reason code.
- **Cost is measured.** Every LLM call logs tokens and estimated cost against the prompt version that produced it.

Full spec in [`PRD.md`](PRD.md).

---

## 1. Working outputs

- 5 four-touch outreach campaigns across 4 accounts, covering all 4 contact-role categories and 3 account categories: [`outputs/messages/`](outputs/messages/)
- 2 full briefings, one multi-site operator and one zero-site edge case: [`outputs/briefings/`](outputs/briefings/)
- A dashboard of all outputs: open [`outputs/dashboard.html`](outputs/dashboard.html)

---

## 2. The workflow

### How I built it

I didn't write this as a finished plan on day one:

1. **Pen and paper.** A first pass at the workflow, the tools and the open questions, before touching a keyboard.
2. **Planning with Claude.** I worked through the architecture, data model and prompting approach, with several rounds of deliberate critique to pressure-test scope and catch mistakes before they became build time.
3. **A settled end product.** A working thin-slice pipeline, not a full send-automation platform, with the scope boundaries and reasoning written down.
4. **PRD.** Every decision consolidated into one spec, treated as the source of truth from then on.
5. **Cursor, plan mode.** The PRD went in as context and Cursor produced a file-by-file implementation checklist.
6. **Build.** Executed against that plan.
7. **Debug and iterate.** Real gaps surfaced once code existed, such as a schema field that was never wired up and a wrong assumption about a third-party API. I fixed them as they came up.

### How it runs

Three layers feed one shared SQLite context store. Two generation paths read from it, then a human review gate decides what moves forward. The key distinction is which steps are deterministic, which call external APIs without generating anything, and which call an LLM. That keeps cost and risk where they belong.

**Deterministic, no API calls, all 120 accounts.** The CSV is parsed into account and contact records. Account-level value-prop candidates are ranked from company facts (visits, category, sites), then weighted per contact by role. This is plain Python in `load_data.py` and `context_builder.py`. It costs nothing and runs on the full list before anything expensive happens.

**External data, not generative.** Apollo is template-only and not wired into the main flow.
- `find_contact_email.py` does a People Match lookup with a strict gate: the email must be verified and the domain must match, or nothing lands in `contacts.resolved_email`. Emails are send-time metadata and never enter LLM context.
- `apollo_push_campaign.py` pushes an approved campaign's subject and bodies onto an Apollo contact's custom fields for a shared sequence template. It does not enrol, activate or send.
- Both scripts are dry-run by default and decoupled from generation and review. They prove the mechanism works without being a live send path.

**Claude Sonnet 5: three call sites, one with web search.**

| Step | Script | Web search? |
|---|---|---|
| Enrichment | `enrich_account.py` → `research.py` | Yes, up to 3 searches per run. Cited facts are saved to `research_evidence`. Runs on demand for demo accounts only. |
| Campaign generation | `generate_message.py` | No. Reads the latest completed dossier and produces a 4-touch campaign. |
| Briefing generation | `generate_briefing.py` | No. Reads the same dossier for that account. |

One dossier per account serves both the campaign and the briefing. Generators refuse a dossier older than seven days. When it's stale, you re-run `enrich_account.py` and regenerate instead of researching inside the generators. Fresh cited enrichment can override the deterministic value-prop default, and the final choice is logged in `drafts.value_prop_used` and `drafts.value_prop_reason`.

Guardrails apply to both generation paths: a never-say list, and every claim traced to something in context. Campaigns also get post-generation checks for banned phrases and per-touch word limits. Every call logs input tokens, output tokens and estimated cost against the prompt version, so unit cost per message, per briefing and per enrichment run is a queryable number.

**The review gate.** `review.py` is the only human gate in the built system. Nothing leaves `pending_review` without an explicit approve, edit or reject.
- Rejecting requires one of eight reason codes: generic, incorrect claim, weak value case, wrong role angle, poor tone, too long, compliance risk, other. Approving or editing doesn't ask for one.
- Campaigns are reviewed per touch, with all four shown together for narrative context. The original `ai_*` content stays immutable. Edits land in `current_*` fields and `draft_events`.
- Briefings are reviewed as a whole draft.

**What happens after approval is designed, not built live.** An approved campaign can be pushed to Apollo's custom fields with `apollo_push_campaign.py`, but enrolment and sending stay in Apollo. There's no poller, and nothing calls it automatically from `review.py`. Approved briefings have no live email dispatch in this repo. Delivery is documented in [`design/send_sequence_design.md`](design/send_sequence_design.md) (Apollo plus transactional email). It's out of scope because a live send flow wouldn't produce evidence on the open risks.

**Explicitly excluded:** sending to real people, LLM generation across all 120 accounts, and individual-level web research on synthetic contacts. The evidence bar is about 5 four-touch campaigns and 2 briefings, not shallow coverage of everything.

### Tools and why

- **Cursor** for the build. Agentic, file-based editing suited a multi-file repo with a real schema better than a notebook.
- **Claude**, in two roles. First as a planning and architecture partner (the PRD, the schema, the critique rounds that caught mistakes before they were built). Second as Claude Sonnet 5 inside the pipeline, doing the generation. I chose it for strong instruction-following on tone and negative constraints, which matters more here than raw benchmark performance given how much of the prompt is guardrails.
- **My own Obsidian knowledge base.** Notes from prior work and sales frameworks (Alex Hormozi's material among them) shaped the tone and structure of the outreach.
- **Apollo** for contact and email sourcing. It has an official MCP server, which raises a real build-versus-buy question about connecting it directly instead of building a custom integration layer. I've flagged that as a decision to make deliberately.

---

## 3. Thinking

### Key trade-offs

- **SQLite for the backend, never for anything human-facing.** These were always two separate decisions. SQLite keeps the data correct: transactions, an append-only audit trail (`draft_events`) and validated state changes (`ALLOWED_TRANSITIONS`). What reps and ops actually look at is a layer on top, and it should depend on the team's real tools. `dashboard.html` is the stand-in here. For a team on Google Workspace, a Google Sheet reading from the same backend is the natural next layer.
- **A shared research dossier, not two independent research calls.** The original design researched separately for every message and every briefing, because they have different freshness needs. Once generation became rep-triggered, one shared mechanism with a freshness policy per command (tight for briefings, looser for messages) did the same job with less research spend.
- **Apollo over a custom script or Resend.** Apollo's sequence engine already handles send, wait, reply detection and auto-stop on reply. Rebuilding that with Resend and custom logic would reimplement something purpose-built tools do reliably. I traded some control over the mechanics for time saved and a verified integration pattern (custom dynamic variables).

### If I had more time

- **Prompt engineering.** The prompts work, but they haven't been iterated against real output variance the way something shipping to reps would need.
- **A proper build-versus-buy review of sales engagement tools, in two steps.** First, whether Apollo is the right choice against other tools or a custom build. I defaulted to it without validating. Second, once a tool is chosen, what its MCP support enables and which of those uses genuinely fit.
- **The real end-user experience.** Slack and similar tools were working assumptions. Where this belongs in a rep's day needs to be learned from reps and ops, not assumed from the platform.
- **A deeper feedback loop.** The pipeline captures a light signal per draft: approve, edit or reject, a reason code on reject, and an edit diff. That's a thin proxy for whether reps trust the output. I'd add conversations with reps using it before treating the signal as sufficient.
- **Aggregate cost tracking.** Per-asset logging exists. What's missing is the layer on top: an aggregate view, a budget, and alerts that catch a cost regression before it's already happened.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY
```

`data/lbe_target_accounts.csv` is a sample dataset of 120 accounts, and `PRD.md` is in the repo root.

## Run order

Run every command from the repo root as `python src/<script>.py`. See the import note in PRD §18 (bare sibling imports, no `-m`, no `__init__.py`).

```bash
# 1. Data and context: no API calls, all 120 accounts
python src/load_data.py
python src/context_builder.py

# 2. Enrichment: one cited dossier per demo account
python src/enrich_account.py --all-demo

# 3. Outreach campaigns: 5 four-touch campaigns, reads the dossier, no web search
python src/generate_message.py

# 4. Briefings: 2 briefings, reuses the same dossier, no web search
python src/generate_briefing.py

# Optional: one account or contact instead of the full demo sample
python src/enrich_account.py --account "Hollywood Bowl"
python src/generate_message.py --account "Puttshack" --contact "Zoe Young"
python src/generate_briefing.py --account "Hollywood Bowl"
python src/generate_briefing.py --force --account "Hollywood Bowl"

# 5. Review: approve, edit or reject (campaigns per touch, briefings whole)
python src/review.py

# 6. Dashboard
python src/dashboard.py
# then open outputs/dashboard.html
```
