"""Task A — 4-touch outreach campaign generator (PRD §5 Stages 3-6, §6, §9, §10, §12, §13).

Stage 5 (generation) here never does its own DB lookup or research — it only
ever sees what context_builder.assemble_message_context() already assembled.
Research is no longer triggered inline: this script reads the account's
latest completed enrichment run via research.get_latest_enrichment(), written
ahead of time by `python src/enrich_account.py`. No web search happens here.

Each run produces one 4-touch email campaign (JSON) stored as a single draft.

Run: python src/generate_message.py   (from repo root; all demo samples)
     python src/generate_message.py --account "Puttshack" --contact "Zoe Young"
"""

import argparse
import json
import sqlite3

from context_builder import (
    DEMO_MESSAGES,
    account_context_xml,
    assemble_message_context,
    find_banned_phrases,
    get_account_by_name,
    get_contact_by_name,
    render_research_sources,
    xml_attr,
    xml_text,
)
from db import DB_PATH, REPO_ROOT, get_connection, report_failures, run_batch
from queue import ensure_schema, insert_campaign_touches
from research import MODEL, estimate_cost, get_client, get_latest_enrichment

PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "campaign_prompt.md"
OUTPUT_DIR = REPO_ROOT / "outputs" / "messages"

PROMPT_VERSION = "campaign_v2"
DRAFT_TYPE = "outreach_campaign"

API_TIMEOUT_SECONDS = 120.0

EXPECTED_DELAYS = {1: 0, 2: 4, 3: 7, 4: 14}
# post-check tolerance per touch — wider than prompt targets
TOUCH_WORD_LIMITS = {
    1: (60, 160),
    2: (30, 100),
    3: (50, 120),
    4: (30, 80),
}

CAMPAIGN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value_prop_used", "value_prop_reason", "touches"],
    "properties": {
        "value_prop_used": {"type": "string"},
        "value_prop_reason": {"type": "string"},
        "touches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step", "delay_days", "channel", "subject", "body"],
                "properties": {
                    "step": {"type": "integer"},
                    "delay_days": {"type": "integer"},
                    "channel": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        },
    },
}


def _build_context_xml(context: dict) -> str:
    account = context["account"]
    contact = context["contact"]
    props_xml = "\n".join(
        f'    <value_prop><name>{xml_text(p["name"])}</name>'
        f'<description>{xml_text(p["description"])}</description></value_prop>'
        for p in context["relevant_value_props"]
    )
    evidence_xml = "\n".join(
        f'    <claim source_url={xml_attr(e["source_url"])}>{xml_text(e["claim"])}</claim>'
        for e in context["research_evidence"]
    ) or "    <!-- no research evidence found for this account -->"

    return f"""{account_context_xml(account)}
<contact_context>
    <name>{xml_text(contact["contact_name"])}</name>
    <title>{xml_text(contact["contact_title"])}</title>
    <role_category>{xml_text(contact["contact_category"])}</role_category>
</contact_context>
<relevant_value_props>
{props_xml}
</relevant_value_props>
<candidate_value_props_ranked>{xml_text(", ".join(context["candidate_value_props_ranked"]))}</candidate_value_props_ranked>
<role_default_value_props>{xml_text(", ".join(context["role_default_value_props"]))}</role_default_value_props>
<role_weighted_value_props>{xml_text(", ".join(context["role_weighted_value_props"]))}</role_weighted_value_props>
<research_evidence>
{evidence_xml}
</research_evidence>"""


def parse_campaign_json(text: str) -> dict:
    parsed = json.loads(text)
    for key in ("value_prop_used", "value_prop_reason", "touches"):
        if key not in parsed:
            raise ValueError(f"campaign output missing key: {key!r}")
    if not isinstance(parsed["touches"], list) or len(parsed["touches"]) != 4:
        raise ValueError(f"touches must contain exactly 4 items, got {len(parsed.get('touches', []))}")

    steps = []
    for touch in parsed["touches"]:
        for field in ("step", "delay_days", "channel", "subject", "body"):
            if field not in touch:
                raise ValueError(f"touch {touch.get('step')} missing field: {field!r}")
        if touch["channel"] != "email":
            raise ValueError(f"touch {touch['step']}: channel must be 'email', got {touch['channel']!r}")
        if not touch["subject"].strip() or not touch["body"].strip():
            raise ValueError(f"touch {touch['step']}: subject and body must be non-empty")
        steps.append(touch["step"])

    if sorted(steps) != [1, 2, 3, 4]:
        raise ValueError(f"touches must have steps 1–4, got {sorted(steps)}")

    for touch in parsed["touches"]:
        step = touch["step"]
        if touch["delay_days"] != EXPECTED_DELAYS[step]:
            raise ValueError(
                f"touch {step}: delay_days must be {EXPECTED_DELAYS[step]}, got {touch['delay_days']}"
            )

    parsed["touches"] = sorted(parsed["touches"], key=lambda t: t["step"])
    return parsed


def check_campaign_guardrails(parsed: dict) -> list[str]:
    """Deterministic code check — violations are printed; draft still proceeds."""
    violations = []
    for touch in parsed["touches"]:
        step = touch["step"]
        body = touch["body"]
        word_count = len(body.split())
        min_words, max_words = TOUCH_WORD_LIMITS[step]
        if not (min_words <= word_count <= max_words):
            violations.append(
                f"touch {step} word count {word_count} outside [{min_words}, {max_words}]"
            )
        for phrase in find_banned_phrases(body):
            violations.append(f"touch {step} banned phrase found: {phrase!r}")
    return violations


def render_campaign_markdown(parsed: dict) -> str:
    lines = [
        f"**Primary value prop:** {parsed['value_prop_used']}",
        f"**Reason:** {parsed['value_prop_reason']}",
        "",
    ]
    for touch in parsed["touches"]:
        lines.extend([
            f"### Touch {touch['step']} (day {touch['delay_days']})",
            "",
            f"**Subject:** {touch['subject']}",
            "",
            touch["body"],
            "",
        ])
    return "\n".join(lines).rstrip()


def format_draft_content(content: str, draft_type: str) -> str:
    """Render stored content for human review."""
    if draft_type == DRAFT_TYPE:
        return render_campaign_markdown(parse_campaign_json(content))
    return content


def _existing_draft(
    conn: sqlite3.Connection, account_id: int, contact_id: int, enrichment_run_id: int
) -> int | None:
    row = conn.execute(
        """
        SELECT draft_id FROM drafts
        WHERE account_id = ? AND contact_id = ?
          AND draft_type = ? AND prompt_version = ?
          AND enrichment_run_id = ?
        ORDER BY draft_id DESC LIMIT 1
        """,
        (account_id, contact_id, DRAFT_TYPE, PROMPT_VERSION, enrichment_run_id),
    ).fetchone()
    return row["draft_id"] if row else None


def generate_one(
    conn: sqlite3.Connection,
    account_id: int,
    contact_id: int,
    *,
    force: bool = False,
) -> int:
    run, research_rows = get_latest_enrichment(conn, account_id)
    enrichment_run_id = run["enrichment_run_id"]

    existing = _existing_draft(conn, account_id, contact_id, enrichment_run_id)
    if existing is not None and not force:
        print(
            f"Skipping existing draft_id={existing} (account_id={account_id}, "
            f"contact_id={contact_id}, enrichment_run_id={enrichment_run_id})"
        )
        return existing

    context = assemble_message_context(conn, account_id, contact_id, research_rows)

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_content = _build_context_xml(context)

    print(
        f"Generating 4-touch campaign for account_id={account_id} contact_id={contact_id}...",
        flush=True,
    )
    client = get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "disabled"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": CAMPAIGN_SCHEMA,
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
    parsed = parse_campaign_json(text)

    allowed = {p["name"] for p in context["relevant_value_props"]}
    if parsed["value_prop_used"] not in allowed:
        raise ValueError(
            f"Model selected unavailable value prop {parsed['value_prop_used']!r}; "
            f"allowed={sorted(allowed)}"
        )

    violations = check_campaign_guardrails(parsed)
    if violations:
        print(f"[GUARDRAIL VIOLATION] account_id={account_id} contact_id={contact_id}: {violations}")

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = estimate_cost(input_tokens, output_tokens)

    content = json.dumps(parsed, indent=2)
    ensure_schema(conn)
    cursor = conn.execute(
        """
        INSERT INTO drafts (
            account_id, contact_id, draft_type, content,
            value_prop_used, value_prop_reason, prompt_version, model,
            input_context, input_tokens, output_tokens, estimated_cost_usd, status,
            enrichment_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)
        """,
        (
            account_id,
            contact_id,
            DRAFT_TYPE,
            content,
            parsed["value_prop_used"],
            parsed["value_prop_reason"],
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
    insert_campaign_touches(conn, draft_id, parsed["touches"])

    account_name = context["account"]["account_name"]
    contact_name = context["contact"]["contact_name"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{draft_id}_{account_name.replace(' ', '_')}_{contact_name.replace(' ', '_')}.md"
    sources = render_research_sources(research_rows, heading="Research sources")
    campaign_md = render_campaign_markdown(parsed)
    out_path.write_text(
        f"# {account_name} -> {contact_name} ({context['contact']['contact_category']})\n\n"
        f"4-touch outreach campaign\n\n"
        f"{campaign_md}\n\n"
        f"{sources}\n\n"
        f"---\n"
        f"tokens: {input_tokens} in / {output_tokens} out — est. cost ${cost:.4f} (generation only)\n"
        f"enrichment_run_id: {enrichment_run_id} (completed_at: {run['completed_at']})\n",
        encoding="utf-8",
    )
    print(
        f"draft_id={draft_id}: {account_name} -> {contact_name} "
        f"({parsed['value_prop_used']}, 4 touches)"
    )
    return draft_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate 4-touch outreach campaigns from the latest enrichment dossier"
    )
    parser.add_argument(
        "--account",
        help='Account name, exact match (e.g. "Puttshack"). Requires --contact.',
    )
    parser.add_argument(
        "--contact",
        help='Contact name, exact match (e.g. "Zoe Young"). Requires --account.',
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate even if a campaign draft already exists for the current enrichment run",
    )
    args = parser.parse_args()

    if (args.account is None) ^ (args.contact is None):
        parser.error("--account and --contact must be used together")

    targets = (
        [(args.account, args.contact)]
        if args.account
        else DEMO_MESSAGES
    )

    conn = get_connection(DB_PATH)

    def _generate(pair: tuple[str, str]) -> int:
        account_name, contact_name = pair
        account = get_account_by_name(conn, account_name)
        contact = get_contact_by_name(conn, account["account_id"], contact_name)
        return generate_one(conn, account["account_id"], contact["contact_id"], force=args.force)

    draft_ids, failures = run_batch(
        targets, _generate, item_name=lambda pair: f"{pair[0]}/{pair[1]}"
    )

    conn.close()
    print(f"\nGenerated {len(draft_ids)} outreach campaigns: draft_ids={draft_ids}")
    report_failures(failures)


if __name__ == "__main__":
    main()
