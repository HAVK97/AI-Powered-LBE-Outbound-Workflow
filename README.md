# AI-Powered LBE Outbound Workflow

Drafts researched, cited cold-email campaigns and pre-call briefings for a B2B sales team that sells to location-based entertainment (LBE) operators, such as mini golf, bowling and immersive theatre venues. Every draft waits in a review queue until a person approves, edits or rejects it.

**Result:** from a 120-account list, it produced 5 four-touch campaigns and 2 briefings for $0.83 of model spend in total, about $0.17 per approved asset. Research cost about $0.14 per account and each draft about $0.02. The figures come from the run logged in [`outputs/dashboard.html`](outputs/dashboard.html).

Built as a time-boxed design exercise. Nothing is sent to anyone.

## Outputs

- [`outputs/messages/`](outputs/messages/): 5 four-touch campaigns across 4 accounts, covering 4 contact roles and 3 venue categories
- [`outputs/briefings/`](outputs/briefings/): 2 briefings, one for a multi-site operator and one for a touring business with no fixed site
- [`outputs/dashboard.html`](outputs/dashboard.html): pipeline status, drafts, research sources, review audit trail and costs

## How it works

| Step | Script | Model or API call | Runs on |
|---|---|---|---|
| 1. Load | `load_data.py` | None | CSV into SQLite: 120 accounts, 250 contacts |
| 2. Score | `context_builder.py` | None | Ranks value propositions per account and per contact role, all 120 accounts |
| 3. Research | `enrich_account.py` → `research.py` | Claude with web search, up to 3 searches | Demo accounts only. Saves cited claims to `research_evidence` |
| 4. Campaigns | `generate_message.py` | Claude, no search | One 4-touch campaign per demo contact |
| 5. Briefings | `generate_briefing.py` | Claude, no search | One briefing per demo account |
| 6. Review | `review.py` | None | Every draft. The only route out of `pending_review` |
| 7. Report | `dashboard.py` | None | Writes `outputs/dashboard.html` |
| Optional | `find_contact_email.py`, `apollo_push_campaign.py` | Apollo REST API | Dry run by default. Never enrols or sends |

## Key design decisions

- **Deterministic first, model last.** Loading and scoring are plain Python and cost nothing, so they run on every account. The model only runs on the demo sample. Trade-off: the rule-based ranking is crude, so the model may pick a different value proposition when the research supports it. It must say why, and the choice is stored in `drafts.value_prop_used` and `drafts.value_prop_reason`.
- **One cited dossier per account.** The campaign and the briefing read the same research run. Generators refuse a dossier older than 7 days and print the command to refresh it. Trade-off: a briefing can use research up to a week old.
- **Python does the maths, the model narrates.** Value-case figures are computed in `compute_value_case()` and passed in as fixed numbers. The vendor's own revenue estimate never enters a prompt.
- **Guardrails checked after generation.** Prompts carry a never-say list. After generation, code checks each touch for banned phrases and word limits (60 to 160 words for touch 1, shorter for follow-ups) and prints any violation for the reviewer. It flags rather than blocks, so the human stays the gate.
- **A real review gate.** Draft status moves through a fixed state machine (`ALLOWED_TRANSITIONS` in `queue.py`). Campaigns are reviewed per touch. The model's original text stays unchanged in `ai_*` fields, edits go to `current_*`, and every action is logged to `draft_events`. Rejecting needs one of 8 reason codes, which turns reviews into feedback data.
- **Cost logged per call.** Tokens, web searches and estimated cost are stored against the prompt version, so cost per asset is a query.
- **Send is designed, not built.** Apollo's sequences already handle sending, waiting and stopping on reply, so the repo only pushes approved copy into Apollo custom fields. Trade-off: no live send loop, so no reply data yet.
- **SQLite and a CLI, not a web app.** Enough for one reviewer and a full audit trail. A team would want a shared front end on the same tables.

## Stack

Python (tested on 3.13), Anthropic SDK (`claude-sonnet-5` with the server-side web search tool), SQLite, python-dotenv. Optional Apollo calls use the standard library's `urllib`, because `src/queue.py` shadows the standard `queue` module and breaks `requests`.

## Repo layout

```
data/lbe_target_accounts.csv   120-account sample (real venue brands, contact names treated as synthetic)
src/                           pipeline scripts, schema.sql and prompts/
design/apollo_campaign_push.md how approved copy would reach Apollo
outputs/                       generated campaigns, briefings and dashboard
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `ANTHROPIC_API_KEY` in `.env` for steps 3 to 5. The `APOLLO_*` variables are only needed for `--live` Apollo calls.

## Run

Run every script from the repo root as `python src/<script>.py`. The scripts use plain sibling imports, so `python -m` does not work. The database is created at `db/outbound.db`.

```bash
# No API calls
python src/load_data.py            # add --reset to wipe research and drafts and reload
python src/context_builder.py

# Claude calls
python src/enrich_account.py --all-demo
python src/generate_message.py
python src/generate_briefing.py

# Review in the terminal, then rebuild the dashboard
python src/review.py
python src/dashboard.py

# Single account or contact
python src/enrich_account.py --account "Hollywood Bowl"
python src/generate_message.py --account "Puttshack" --contact "Zoe Young"
python src/generate_briefing.py --account "Hollywood Bowl" --force

# Optional Apollo templates (dry run unless --live)
python src/find_contact_email.py --all-demo
python src/apollo_push_campaign.py --draft-id 1
```

There are no automated tests yet.

## What I'd do next

- Add tests for the review state machine and the post-generation checks.
- Iterate the prompts against real output variance and reviewer reason codes.
- Watch reps use it before trusting approve and edit rates as the quality signal.
- Add an aggregate cost view with a budget and alerts.
