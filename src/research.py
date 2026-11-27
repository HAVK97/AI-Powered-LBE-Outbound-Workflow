"""Stage 3 — enrichment (PRD §5, §11). Enrichment is triggered on demand via
`enrich_account.py`, independent of generation. Each call is a full Claude API
request with Anthropic's built-in server-side web search tool. Claude decides
when to search, runs the query, and returns cited results; this module
extracts {claim, source_url} pairs from those citations and writes them to
research_evidence, grouped under one `enrichment_runs` row per call.

One company dossier is shared by both outreach messages and briefings for
that account — there is no longer a message_draft/briefing_prep split.
Freshness before a call is handled by re-running `enrich_account.py` (each run
always performs web search and appends a new row). Generation reads the latest
*completed* run via `get_latest_enrichment()` and may refuse stale dossiers
older than DEFAULT_MAX_AGE_HOURS unless re-enriched.

Individual-level research is deliberately excluded (§2, §11) — this only ever
searches for the account/company, never a named contact.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic
from dotenv import load_dotenv

from db import add_column_if_missing

load_dotenv()

MODEL = "claude-sonnet-5"
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 3,
    # Sonnet 5's default dynamic-filtering path returned search results but no
    # citation objects. Direct calling preserves Anthropic's built-in citations.
    "allowed_callers": ["direct"],
}
# Introductory token pricing through Aug 31 2026 (PRD §12); $10/1k searches (PRD §11)
PRICE_IN_PER_MTOK = 2.0
PRICE_OUT_PER_MTOK = 10.0
PRICE_PER_SEARCH = 0.01
API_TIMEOUT_SECONDS = 120.0

ENRICH_PROMPT_VERSION = "enrich_v1"
# Generators refuse dossiers older than this unless re-enriched.
DEFAULT_MAX_AGE_HOURS = 168  # 7 days

_client: Anthropic | None = None


def get_client() -> Anthropic:
    """Shared Anthropic client, reused by enrich_account.py, generate_message.py
    and generate_briefing.py so every call in the pipeline goes through one place.
    """
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in")
        _client = Anthropic(api_key=api_key, timeout=API_TIMEOUT_SECONDS)
    return _client


class EnrichmentError(RuntimeError):
    """Base class for enrichment lookup failures raised to generators."""


class MissingEnrichmentError(EnrichmentError):
    """No completed enrichment run exists yet for this account."""


class StaleEnrichmentError(EnrichmentError):
    """The latest completed enrichment run is older than max_age_hours."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migration for enrichment_runs + enrichment_run_id columns.

    schema.sql already covers a fresh database; this covers upgrading an
    existing db/outbound.db created before this table existed. Safe to call on
    every startup of enrich/generate/dashboard.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichment_runs (
            enrichment_run_id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(account_id),
            status TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            web_search_requests INTEGER,
            estimated_cost_usd REAL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_enrichment_runs_lookup "
        "ON enrichment_runs(account_id, status, completed_at)"
    )
    add_column_if_missing(
        conn, "research_evidence", "enrichment_run_id",
        "INTEGER REFERENCES enrichment_runs(enrichment_run_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_evidence_run "
        "ON research_evidence(account_id, enrichment_run_id)"
    )
    add_column_if_missing(
        conn, "drafts", "enrichment_run_id",
        "INTEGER REFERENCES enrichment_runs(enrichment_run_id)",
    )
    conn.commit()


def _extract_claims(response) -> list[dict]:
    """Pull {claim, source_url} pairs out of a web-search-grounded response.

    The claim is the model's grounded text statement; the URL comes from the
    citation (url for web_search_result_location, source for search_result_location).
    Source excerpts (cited_text) are not stored as claims.
    """
    claims: list[dict] = []
    seen: set[tuple[str, str]] = set()
    data = response.model_dump()

    for block in data.get("content", []):
        if block.get("type") != "text":
            continue
        claim_text = (block.get("text") or "").strip()
        if not claim_text:
            continue
        for citation in block.get("citations") or []:
            url = citation.get("url") or citation.get("source")
            if not url:
                continue
            key = (claim_text, url)
            if key in seen:
                continue
            seen.add(key)
            claims.append({"claim": claim_text, "source_url": url})
    return claims


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Generation-only token cost — also used by generate_message.py and
    generate_briefing.py so the Aug 31 2026 pricing cutover (PRD §12) only
    needs updating in one place.
    """
    return (input_tokens / 1_000_000) * PRICE_IN_PER_MTOK + (output_tokens / 1_000_000) * PRICE_OUT_PER_MTOK


def _usage_dict(*, input_tokens: int = 0, output_tokens: int = 0, web_search_requests: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "web_search_requests": web_search_requests,
        "estimated_cost_usd": estimate_cost(input_tokens, output_tokens) + web_search_requests * PRICE_PER_SEARCH,
    }


def fetch_company_dossier(account: dict) -> tuple[list[dict], dict]:
    """Call Claude with web_search for one account. No database writes here —
    this is pure fetch; see create_enrichment_run() for persistence.

    Raises RuntimeError if the response is truncated or returns no cited
    claims — refuses to hand back an evidence-free dossier.
    """
    prompt = (
        f"Research the company \"{account['account_name']}\" "
        f"({account['category']} / {account['sub_category']}, "
        f"HQ: {account['hq_location']}). "
        "Return 3-5 concise, standalone, current facts useful for B2B sales "
        "outreach or a pre-call briefing, with a source citation attached to "
        "every fact. Prioritise recent news, expansion or new site openings, notable "
        "partnerships, corporate/group booking activity, funding, leadership "
        "changes, or anything that signals what matters to them right now. "
        "Use web_search. State only facts you can support with a cited source. "
        "If you find nothing specific and current, say so plainly rather than guessing."
    )

    print(
        f"fetch_company_dossier({account['account_name']!r}): starting web search...",
        flush=True,
    )

    client = get_client()
    # thinking disabled: this is extraction, not a reasoning workload. Sonnet 5
    # enables adaptive thinking by default and max_tokens covers thinking+text —
    # leaving it on caused truncated / empty cited outputs.
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "disabled"},
        tools=[WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": prompt}],
        timeout=API_TIMEOUT_SECONDS,
    )

    search_requests = 0
    if response.usage and getattr(response.usage, "server_tool_use", None):
        search_requests = response.usage.server_tool_use.web_search_requests or 0

    print(
        f"fetch_company_dossier({account['account_name']!r}): "
        f"stop_reason={response.stop_reason} "
        f"output_tokens={response.usage.output_tokens} "
        f"web_search_requests={search_requests}",
        flush=True,
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"enrichment truncated for {account['account_name']!r}: "
            f"stop_reason=max_tokens, output_tokens={response.usage.output_tokens}"
        )

    claims = _extract_claims(response)
    if not claims:
        raise RuntimeError(
            f"Enrichment for {account['account_name']!r} made {search_requests} web "
            f"search request(s) but returned no cited claims. Refusing to store an "
            f"evidence-free dossier."
        )

    usage = _usage_dict(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        web_search_requests=search_requests,
    )
    return claims, usage


def _latest_completed_run(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM enrichment_runs
        WHERE account_id = ? AND status = 'completed'
        ORDER BY completed_at DESC, enrichment_run_id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()


def _is_fresh(run: sqlite3.Row, max_age_hours: int) -> bool:
    completed_at = run["completed_at"]
    if not completed_at:
        return False
    completed_dt = datetime.strptime(completed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - completed_dt <= timedelta(hours=max_age_hours)


def create_enrichment_run(
    conn: sqlite3.Connection,
    account_id: int,
) -> int:
    """Run web search for one account and persist a new enrichment run.

    Every call inserts a new 'running' row, fetches a fresh dossier via
    fetch_company_dossier(), and persists claims + run completion. Older
    completed runs for the same account are kept (append-only history).
    On failure the run row is kept with status='failed' and the exception
    re-raised.

    Returns enrichment_run_id.
    """
    ensure_schema(conn)

    account_row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if account_row is None:
        raise ValueError(f"no account with id {account_id}")
    account = dict(account_row)

    cursor = conn.execute(
        """
        INSERT INTO enrichment_runs (account_id, status, prompt_version, model)
        VALUES (?, 'running', ?, ?)
        """,
        (account_id, ENRICH_PROMPT_VERSION, MODEL),
    )
    conn.commit()
    run_id = cursor.lastrowid

    try:
        claims, usage = fetch_company_dossier(account)
    except Exception as e:
        conn.execute(
            "UPDATE enrichment_runs SET status = 'failed', error = ?, "
            "completed_at = CURRENT_TIMESTAMP WHERE enrichment_run_id = ?",
            (str(e), run_id),
        )
        conn.commit()
        raise

    for c in claims:
        conn.execute(
            "INSERT INTO research_evidence (account_id, triggered_by, claim, source_url, enrichment_run_id) "
            "VALUES (?, 'enrich', ?, ?, ?)",
            (account_id, c["claim"], c["source_url"], run_id),
        )
    conn.execute(
        """
        UPDATE enrichment_runs
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
            input_tokens = ?, output_tokens = ?, web_search_requests = ?, estimated_cost_usd = ?
        WHERE enrichment_run_id = ?
        """,
        (
            usage["input_tokens"], usage["output_tokens"],
            usage["web_search_requests"], usage["estimated_cost_usd"],
            run_id,
        ),
    )
    conn.commit()

    print(
        f"create_enrichment_run({account['account_name']!r}): "
        f"enrichment_run_id={run_id} {len(claims)} cited claim(s), "
        f"cost=${usage['estimated_cost_usd']:.4f}",
        flush=True,
    )
    return run_id


def get_latest_enrichment(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> tuple[dict, list[dict]]:
    """Load the latest completed enrichment run + its claims for generation.

    Generators call this instead of running research themselves. Raises
    MissingEnrichmentError if no completed run exists, or StaleEnrichmentError
    if the latest one is older than max_age_hours — both messages tell the
    operator exactly which enrich_account.py command to run.
    """
    ensure_schema(conn)

    run = _latest_completed_run(conn, account_id)
    account_row = conn.execute(
        "SELECT account_name FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    account_name = account_row["account_name"] if account_row else f"account_id={account_id}"

    if run is None:
        raise MissingEnrichmentError(
            f"No completed enrichment for {account_name!r} — run: "
            f'python src/enrich_account.py --account "{account_name}"'
        )
    if not _is_fresh(run, max_age_hours):
        raise StaleEnrichmentError(
            f"Latest enrichment for {account_name!r} (enrichment_run_id="
            f"{run['enrichment_run_id']}, completed_at={run['completed_at']}) is "
            f"older than {max_age_hours}h — re-enrich with: "
            f'python src/enrich_account.py --account "{account_name}"'
        )

    claim_rows = conn.execute(
        "SELECT claim, source_url, retrieved_at FROM research_evidence "
        "WHERE enrichment_run_id = ? ORDER BY evidence_id",
        (run["enrichment_run_id"],),
    ).fetchall()
    return dict(run), [dict(c) for c in claim_rows]
