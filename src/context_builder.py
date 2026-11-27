"""Stage 2 (deterministic scoring, all 120 accounts) and Stage 4 (context
assembly for a single account/contact) — PRD §5, §8, §9, §10.

Note: "Stage 2 scoring" here is distinct from Stage 3 "company enrichment"
(research.py / enrich_account.py) — the former is free, deterministic, and
runs across all 120 accounts; the latter is on-demand and calls Claude.

VALUE_PROPS and ROLE_MAP are fixed constants (§9), not account-specific data.
Both generators import from this module; nothing in Stage 5 (generation) does
its own DB lookups or research — everything it needs is assembled here first.

Run standalone to execute Stage 2 across all 120 accounts:
    python src/context_builder.py
"""

import json
import sqlite3
import statistics
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from db import DB_PATH as DEFAULT_DB_PATH
from db import get_connection

# --- §9: value proposition reference (verbatim — feed the description, not just
# the name, into every prompt) ---
VALUE_PROPS = {
    "Conversion Rate": "Increases conversion rate by 25% through a smoother booking journey and wider payment options (e.g. group split pay, payment plans).",
    "Transaction Value": "Increases average transaction value by 8% through upsell and package options at point of booking.",
    "AI Customer Support": "Agentic AI for Customer Support allows the Creator to run the function at scale without adding team cost, whilst improving CSAT.",
    "AI Sales Agent": "Agentic AI for complex group and corporate sales enquiries — managing these end-to-end, freeing up team time, and increasing win rate.",
    "Cost & Complexity Reduction": "Reduces cost by removing the need for multiple different systems (e.g. separate systems for online booking, walk-ins and group sales). Also reduces operational complexity, freeing up time.",
}

# --- §10: deterministic role -> candidate-prop baseline. CEO/Managing Director
# is not a fixed list here — it's resolved at enrichment time as the top 2
# entries of that account's own candidate_value_props (see role_default_props),
# since Conversion Rate + Transaction Value are always first in rank_value_props
# by construction. No separate $-ranking step exists or is needed. ---
ROLE_MAP = {
    "Commercial": ["Conversion Rate", "Transaction Value", "AI Sales Agent"],
    "Finance": ["Cost & Complexity Reduction", "Transaction Value"],
    "Operations": ["AI Customer Support", "Cost & Complexity Reduction"],
}

CEO_ROLE = "CEO / Managing Director"

AI_SALES_AGENT_CATEGORIES = {"Competitive Socialising", "Immersive Theatre / Dining", "Karaoke"}

# --- §8: hardcoded caveat + currency note. Python owns this wording so it never
# depends on a given generation run. ---
VALUE_CASE_CAVEAT = (
    "These figures follow the brief's own illustrative method (visits x ticket "
    "price x uplift %). A precise ROI model would need current online conversion "
    "rate, traffic, and booking-vs-footfall breakdown, which aren't in this "
    "dataset — worth asking about live."
)
CURRENCY_NOTE = (
    "Figures are in $, matching the source CSV — the brief's own worked example "
    "uses £, a known inconsistency in the provided materials, flagged rather than "
    "silently converted."
)

# --- §6 / §8: frozen demo sample. Names only here; resolved to account_id /
# contact_id against the loaded DB at generation time. ---
DEMO_MESSAGES: list[tuple[str, str]] = [
    ("Puttshack", "Zoe Young"),
    ("Puttshack", "Sian Gibson"),
    ("NQ64", "Aisha Hall"),
    ("Secret Cinema", "Adam Grant"),
    ("Escape Reality", "Katie Jackson"),
]

DEMO_BRIEFINGS: list[str] = ["Hollywood Bowl", "Secret Cinema"]

BANNED_PHRASES = ["revolutionize", "game-changer", "synergy", "leverage our"]


def demo_account_names() -> list[str]:
    """Unique account names across both demo samples, first-seen order.

    Shared by enrich_account.py (--all-demo) and dashboard.py — both used to
    hand-roll this exact dedupe loop separately.
    """
    names = [name for name, _ in DEMO_MESSAGES] + list(DEMO_BRIEFINGS)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def split_name(full_name: str) -> tuple[str, str]:
    """First/last name split shared by find_contact_email.py and
    apollo_push_campaign.py (both send Apollo first_name/last_name params).
    """
    parts = full_name.strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def is_touring_business(sites: int) -> bool:
    """§9 rule: no fixed sites = touring business. One definition, used at
    both load time (load_data.py) and re-derived at Stage 2 (enrich_all()).
    """
    return sites == 0


def find_banned_phrases(text: str) -> list[str]:
    lowered = text.lower()
    return [p for p in BANNED_PHRASES if p in lowered]


def account_context_xml(account: dict) -> str:
    """The <account_context> block shared verbatim by generate_message.py's
    and generate_briefing.py's prompt XML — same 9 fields, same order.
    """
    return f"""<account_context>
    <name>{xml_text(account["account_name"])}</name>
    <category>{xml_text(account["category"])}</category>
    <sub_category>{xml_text(account["sub_category"])}</sub_category>
    <description>{xml_text(account["description"])}</description>
    <hq_location>{xml_text(account["hq_location"])}</hq_location>
    <region>{xml_text(account["region"])}</region>
    <sites>{account["sites"]}</sites>
    <is_touring_business>{account["is_touring_business"]}</is_touring_business>
    <annual_visits>{account["annual_visits"]}</annual_visits>
</account_context>"""


def render_research_sources(research_rows: list[dict], heading: str = "Research Sources") -> str:
    lines = [f"## {heading}", ""]
    for row in research_rows:
        lines.append(f"- {row['claim']}")
        lines.append(f"  {row['source_url']}")
    return "\n".join(lines)


def rank_value_props(account: dict, median_visits: float) -> list[str]:
    """§10 — account-level candidate ranking, deterministic, cheap."""
    props = ["Conversion Rate", "Transaction Value"]  # always relevant
    if account["annual_visits"] > median_visits:
        props.append("AI Customer Support")
    if account["category"] in AI_SALES_AGENT_CATEGORIES:
        props.append("AI Sales Agent")
    if account["sites"] >= 3 or account["is_touring_business"]:
        props.append("Cost & Complexity Reduction")
    return props


def role_default_props(contact_category: str, candidate_value_props: list[str]) -> list[str]:
    """§10 — deterministic baseline per contact role.

    CEO / Managing Director has no fixed list: it's the top 2 entries of that
    account's own candidate_value_props (Conversion Rate + Transaction Value by
    construction of rank_value_props), not a separate $-ranking step.
    """
    if contact_category == CEO_ROLE:
        return candidate_value_props[:2]
    return ROLE_MAP.get(contact_category, [])


def compute_value_case(account: dict) -> dict:
    """§8 — value-case figures. Python calculates, the model only narrates.
    vendor_annual_revenue is deliberately excluded: it's the vendor's own take, never
    Creator-facing output. annual_visits / avg_ticket_price are included so
    render can show the brief's visits × ticket formula without model math.
    """
    annual_visits = account["annual_visits"]
    avg_ticket_price = account["avg_ticket_price"]
    current_transaction_volume = annual_visits * avg_ticket_price
    return {
        "annual_visits": annual_visits,
        "avg_ticket_price": avg_ticket_price,
        "current_transaction_volume": current_transaction_volume,
        "illustrative_conversion_uplift": current_transaction_volume * 0.25,
        "illustrative_transaction_value_uplift": current_transaction_volume * 0.08,
        "caveat": VALUE_CASE_CAVEAT,
        "currency_note": CURRENCY_NOTE,
    }


def enrich_all(db_path: str = str(DEFAULT_DB_PATH)) -> None:
    """Stage 2 — deterministic enrichment across all 120 accounts, no API cost."""
    conn = get_connection(db_path)
    accounts = [dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()]
    if not accounts:
        raise RuntimeError("No accounts found — run load_data.py first")

    median_visits = statistics.median(a["annual_visits"] for a in accounts)

    for account in accounts:
        total_transaction_volume = account["annual_visits"] * account["avg_ticket_price"]
        is_touring = is_touring_business(account["sites"])
        candidates = rank_value_props({**account, "is_touring_business": is_touring}, median_visits)
        conn.execute(
            """
            UPDATE accounts
            SET total_transaction_volume = ?,
                is_touring_business = ?,
                candidate_value_props = ?
            WHERE account_id = ?
            """,
            (total_transaction_volume, is_touring, json.dumps(candidates), account["account_id"]),
        )

        contacts = conn.execute(
            "SELECT * FROM contacts WHERE account_id = ?", (account["account_id"],)
        ).fetchall()
        for contact in contacts:
            defaults = role_default_props(contact["contact_category"], candidates)
            conn.execute(
                "UPDATE contacts SET role_default_value_props = ? WHERE contact_id = ?",
                (json.dumps(defaults), contact["contact_id"]),
            )

    conn.commit()
    conn.close()
    print(f"Enriched {len(accounts)} accounts (median annual_visits = {median_visits:,.0f})")


def get_account_by_name(conn: sqlite3.Connection, account_name: str) -> dict:
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_name = ?", (account_name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No account named {account_name!r}")
    return dict(row)


def get_contact_by_name(conn: sqlite3.Connection, account_id: int, contact_name: str) -> dict:
    row = conn.execute(
        "SELECT * FROM contacts WHERE account_id = ? AND contact_name = ?",
        (account_id, contact_name),
    ).fetchone()
    if row is None:
        raise ValueError(f"No contact named {contact_name!r} on account {account_id}")
    return dict(row)


def get_contacts_for_account(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM contacts WHERE account_id = ?", (account_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _value_props_with_descriptions(names: list[str]) -> list[dict]:
    return [{"name": name, "description": VALUE_PROPS[name]} for name in names]


def role_weighted_props(role_defaults: list[str], candidates: list[str]) -> list[str]:
    """Role defaults first (that also appear on the account shortlist), then any
    remaining account candidates. This is what Task A should treat as the
    default ranking before evidence-informed override (§10).
    """
    role_first = [p for p in role_defaults if p in candidates]
    rest = [p for p in candidates if p not in role_first]
    return role_first + rest


def xml_text(value) -> str:
    return xml_escape("" if value is None else str(value))


def xml_attr(value) -> str:
    # quoteattr includes surrounding quotes
    return quoteattr("" if value is None else str(value))


def assemble_message_context(
    conn: sqlite3.Connection, account_id: int, contact_id: int, research_rows: list[dict]
) -> dict:
    """Stage 4 — the exact object handed to Task A generation. Joins Stage 2's
    deterministic facts with the account's latest company-enrichment evidence
    (from research.get_latest_enrichment(), written ahead of time by
    enrich_account.py), plus contact context (role only — never researched, §2).
    Nothing downstream does its own lookups or research.
    """
    account = dict(conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone())
    contact = dict(conn.execute("SELECT * FROM contacts WHERE contact_id = ?", (contact_id,)).fetchone())

    candidates = json.loads(account["candidate_value_props"] or "[]")
    role_defaults = json.loads(contact["role_default_value_props"] or "[]")
    weighted = role_weighted_props(role_defaults, candidates)

    return {
        "account": {
            "account_id": account["account_id"],
            "account_name": account["account_name"],
            "category": account["category"],
            "sub_category": account["sub_category"],
            "description": account["description"],
            "hq_location": account["hq_location"],
            "region": account["region"],
            "sites": account["sites"],
            "is_touring_business": bool(account["is_touring_business"]),
            "annual_visits": account["annual_visits"],
        },
        "contact": {
            "contact_id": contact["contact_id"],
            "contact_name": contact["contact_name"],
            "contact_title": contact["contact_title"],
            "contact_category": contact["contact_category"],
        },
        "relevant_value_props": _value_props_with_descriptions(weighted),
        "candidate_value_props_ranked": candidates,
        "role_default_value_props": role_defaults,
        "role_weighted_value_props": weighted,
        "research_evidence": research_rows,
    }


def assemble_briefing_context(
    conn: sqlite3.Connection, account_id: int, research_rows: list[dict]
) -> dict:
    """Stage 4 — the exact object handed to Task C generation. Every contact on
    the account (not one designated primary, §8), pre-computed value-case
    figures for narration only, and the account's latest company-enrichment
    evidence — the same dossier already used for that account's outreach
    messages, if any (one dossier serves both tasks; see enrich_account.py).
    """
    account = dict(conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone())
    contacts = get_contacts_for_account(conn, account_id)
    candidates = json.loads(account["candidate_value_props"] or "[]")

    return {
        "account": {
            "account_id": account["account_id"],
            "account_name": account["account_name"],
            "category": account["category"],
            "sub_category": account["sub_category"],
            "description": account["description"],
            "hq_location": account["hq_location"],
            "region": account["region"],
            "sites": account["sites"],
            "is_touring_business": bool(account["is_touring_business"]),
            "annual_visits": account["annual_visits"],
        },
        "contacts": [
            {
                "contact_name": c["contact_name"],
                "contact_title": c["contact_title"],
                "contact_category": c["contact_category"],
            }
            for c in contacts
        ],
        "relevant_value_props": _value_props_with_descriptions(candidates),
        "value_case": compute_value_case(account),
        "research_evidence": research_rows,
    }


def main() -> None:
    enrich_all()


if __name__ == "__main__":
    main()
