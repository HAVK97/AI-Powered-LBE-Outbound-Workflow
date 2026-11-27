"""Task C — briefing note generator (PRD §5 Stages 3-6, §8).

Research is no longer triggered inline: this script reads the account's
latest completed enrichment run via research.get_latest_enrichment() — the
same company dossier already used for that account's outreach messages, if
any. No web search happens here. If the dossier feels stale before a call,
refresh it first with `python src/enrich_account.py --account "..."`.

Run: python src/generate_briefing.py   (from repo root; all demo samples)
     python src/generate_briefing.py --account "Hollywood Bowl"
"""

import argparse
import json
import sqlite3

from context_builder import (
    DEMO_BRIEFINGS,
    VALUE_PROPS,
    account_context_xml,
    assemble_briefing_context,
    find_banned_phrases,
    get_account_by_name,
    render_research_sources,
    xml_attr,
    xml_text,
)
from db import DB_PATH, REPO_ROOT, get_connection, report_failures, run_batch
from research import MODEL, estimate_cost, get_client, get_latest_enrichment

PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "briefing_prompt.md"
OUTPUT_DIR = REPO_ROOT / "outputs" / "briefings"

PROMPT_VERSION = "briefing_v3"
API_TIMEOUT_SECONDS = 120.0

BRIEFING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "company_overview",
        "individual_profile",
        "value_props_to_lead_with",
        "talking_points",
    ],
    "properties": {
        "company_overview": {"type": "string"},
        "individual_profile": {"type": "string"},
        "value_props_to_lead_with": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "why"],
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
        "talking_points": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


def _build_context_xml(context: dict) -> str:
    account = context["account"]
    contacts_xml = "\n".join(
        f'    <contact><name>{xml_text(c["contact_name"])}</name>'
        f'<title>{xml_text(c["contact_title"])}</title>'
        f'<role_category>{xml_text(c["contact_category"])}</role_category></contact>'
        for c in context["contacts"]
    )
    props_xml = "\n".join(
        f'    <value_prop><name>{xml_text(p["name"])}</name>'
        f'<description>{xml_text(p["description"])}</description></value_prop>'
        for p in context["relevant_value_props"]
    )
    evidence_xml = "\n".join(
        f'    <claim source_url={xml_attr(e["source_url"])}>{xml_text(e["claim"])}</claim>'
        for e in context["research_evidence"]
    ) or "    <!-- no research evidence found for this account -->"
    vc = context["value_case"]

    primary = context.get("primary_contact")
    primary_xml = ""
    if primary:
        primary_xml = (
            f"<primary_contact>"
            f'<name>{xml_text(primary["contact_name"])}</name>'
            f'<title>{xml_text(primary["contact_title"])}</title>'
            f'<role_category>{xml_text(primary["contact_category"])}</role_category>'
            f"</primary_contact>\n"
        )

    # Pass figures only — caveat/currency_note are hardcoded in Python render (§8).
    # Do not ask the model to reproduce them.
    return f"""{account_context_xml(account)}
{primary_xml}<contacts>
{contacts_xml}
</contacts>
<relevant_value_props>
{props_xml}
</relevant_value_props>
<value_case>
    <annual_visits>{vc["annual_visits"]}</annual_visits>
    <avg_ticket_price>{vc["avg_ticket_price"]:.2f}</avg_ticket_price>
    <current_transaction_volume>{vc["current_transaction_volume"]:.2f}</current_transaction_volume>
    <illustrative_conversion_uplift>{vc["illustrative_conversion_uplift"]:.2f}</illustrative_conversion_uplift>
    <illustrative_transaction_value_uplift>{vc["illustrative_transaction_value_uplift"]:.2f}</illustrative_transaction_value_uplift>
</value_case>
<research_evidence>
{evidence_xml}
</research_evidence>"""


def parse_briefing_json(text: str) -> dict:
    parsed = json.loads(text)
    required = {
        "company_overview",
        "individual_profile",
        "value_props_to_lead_with",
        "talking_points",
    }
    missing = required - parsed.keys()
    if missing:
        raise ValueError(f"briefing output missing keys: {missing}")
    if not isinstance(parsed["talking_points"], list):
        raise ValueError("talking_points must be a list")
    if not (3 <= len(parsed["talking_points"]) <= 4):
        raise ValueError(f"talking_points must have 3-4 items, got {len(parsed['talking_points'])}")
    for key in ("company_overview", "individual_profile"):
        if not isinstance(parsed[key], str) or not parsed[key].strip():
            raise ValueError(f"{key} must be a non-empty string")

    props = parsed["value_props_to_lead_with"]
    if not isinstance(props, list) or not (1 <= len(props) <= 2):
        raise ValueError("value_props_to_lead_with must be a list of 1-2 objects")
    for i, prop in enumerate(props):
        if not isinstance(prop, dict):
            raise ValueError(f"value_props_to_lead_with[{i}] must be an object")
        name = prop.get("name")
        why = prop.get("why")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"value_props_to_lead_with[{i}].name must be a non-empty string")
        if name not in VALUE_PROPS:
            raise ValueError(
                f"value_props_to_lead_with[{i}].name {name!r} is not a known value prop"
            )
        if not isinstance(why, str) or not why.strip():
            raise ValueError(f"value_props_to_lead_with[{i}].why must be a non-empty string")
    return parsed


def check_briefing_guardrails(parsed: dict) -> list[str]:
    """Deterministic code check (PRD §5 Stage 6) — non-blocking, printed only."""
    prop_text = " ".join(
        f"{p['name']} {p['why']}" for p in parsed["value_props_to_lead_with"]
    )
    full_text = " ".join(
        [
            parsed["company_overview"],
            parsed["individual_profile"],
            prop_text,
            " ".join(parsed["talking_points"]),
        ]
    )
    return [f"banned phrase found: {phrase!r}" for phrase in find_banned_phrases(full_text)]


def render_briefing_markdown(parsed: dict, value_case: dict) -> str:
    # vendor_annual_revenue is the vendor's take — never Creator-facing (§8)
    visits = value_case["annual_visits"]
    ticket = value_case["avg_ticket_price"]
    volume = value_case["current_transaction_volume"]
    conv = value_case["illustrative_conversion_uplift"]
    atv = value_case["illustrative_transaction_value_uplift"]

    lead_lines = "\n".join(
        f"- **{p['name']}** — {p['why']}" for p in parsed["value_props_to_lead_with"]
    )
    talking_points = "\n".join(f"- {tp}" for tp in parsed["talking_points"])

    return f"""## Company overview

{parsed["company_overview"]}

## Individual profile

{parsed["individual_profile"]}

## Value case

With **{visits:,.0f} annual visits** at **${ticket:,.2f} avg ticket**, current transaction volume ≈ **${volume:,.0f}**.
A **25% conversion uplift** → **+${conv:,.0f}**; an **8% ATV uplift** → **+${atv:,.0f}**.

**Lead with:**
{lead_lines}

_{value_case["caveat"]} {value_case["currency_note"]}_

## Talking points

{talking_points}
"""


def _existing_draft(conn: sqlite3.Connection, account_id: int, enrichment_run_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT draft_id FROM drafts
        WHERE account_id = ? AND contact_id IS NULL
          AND draft_type = 'briefing' AND prompt_version = ?
          AND enrichment_run_id = ?
        ORDER BY draft_id DESC LIMIT 1
        """,
        (account_id, PROMPT_VERSION, enrichment_run_id),
    ).fetchone()
    return row["draft_id"] if row else None


def generate_one(conn: sqlite3.Connection, account_id: int, *, force: bool = False) -> int:
    run, research_rows = get_latest_enrichment(conn, account_id)
    enrichment_run_id = run["enrichment_run_id"]

    existing = _existing_draft(conn, account_id, enrichment_run_id)
    if existing is not None and not force:
        print(
            f"Skipping existing draft_id={existing} (account_id={account_id}, "
            f"enrichment_run_id={enrichment_run_id})"
        )
        return existing

    context = assemble_briefing_context(conn, account_id, research_rows)

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_content = _build_context_xml(context)

    print(f"Generating briefing for account_id={account_id}...", flush=True)
    client = get_client()
    # temperature omitted — deprecated / rejected for claude-sonnet-5
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "disabled"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": BRIEFING_SCHEMA,
            }
        },
        timeout=API_TIMEOUT_SECONDS,
    )
    print(
        f"generation stop_reason={response.stop_reason} "
        f"output_tokens={response.usage.output_tokens}",
        flush=True,
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(
            f"Incomplete response: stop_reason={response.stop_reason}, "
            f"output_tokens={response.usage.output_tokens}"
        )

    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = parse_briefing_json(text)

    violations = check_briefing_guardrails(parsed)
    if violations:
        print(f"[GUARDRAIL VIOLATION] account_id={account_id}: {violations}")

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = estimate_cost(input_tokens, output_tokens)

    value_case = context["value_case"]
    markdown = render_briefing_markdown(parsed, value_case)
    markdown = (
        f"{markdown}\n{render_research_sources(research_rows)}\n\n"
        f"---\nenrichment_run_id: {enrichment_run_id} (completed_at: {run['completed_at']})\n"
        f"tokens: {input_tokens} in / {output_tokens} out — est. cost ${cost:.4f} (generation only)\n"
    )

    cursor = conn.execute(
        """
        INSERT INTO drafts (
            account_id, contact_id, draft_type, content,
            value_prop_used, value_prop_reason, prompt_version, model,
            input_context, input_tokens, output_tokens, estimated_cost_usd, status,
            enrichment_run_id
        ) VALUES (?, NULL, 'briefing', ?, NULL, NULL, ?, ?, ?, ?, ?, ?, 'pending_review', ?)
        """,
        (
            account_id,
            markdown,
            PROMPT_VERSION,
            MODEL,
            json.dumps(context),
            input_tokens,
            output_tokens,
            cost,
            enrichment_run_id,
        ),
    )
    conn.commit()
    draft_id = cursor.lastrowid

    account_name = context["account"]["account_name"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{draft_id}_{account_name.replace(' ', '_')}.md"
    out_path.write_text(f"# Briefing: {account_name}\n\n{markdown}", encoding="utf-8")
    print(f"draft_id={draft_id}: briefing for {account_name}")
    return draft_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate briefings from the latest enrichment dossier"
    )
    parser.add_argument(
        "--account",
        help='Account name, exact match (e.g. "Hollywood Bowl"). Omit to run all demo samples.',
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate even if a briefing already exists for the current enrichment run",
    )
    args = parser.parse_args()

    targets = [args.account] if args.account else DEMO_BRIEFINGS

    conn = get_connection(DB_PATH)

    def _generate(account_name: str) -> int:
        account = get_account_by_name(conn, account_name)
        return generate_one(conn, account["account_id"], force=args.force)

    draft_ids, failures = run_batch(targets, _generate)

    conn.close()
    print(f"\nGenerated {len(draft_ids)} briefings: draft_ids={draft_ids}")
    report_failures(failures)


if __name__ == "__main__":
    main()
