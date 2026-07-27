"""Stage 3b — contact email lookup via Apollo (template, PRD §2/§11 caveat).

Person-level enrichment, decoupled from company enrichment (research.py /
enrich_account.py). NOT wired into generate_message.py or generate_briefing.py
— an email address is send-time metadata, never LLM context (context_builder
never reads resolved_email).

This is deliberately template code, not an operationalised capability:
contacts in this dataset are almost certainly synthetic names generated for
the exercise (PRD §2). A "successful" Apollo match against a synthetic name
is more likely a coincidental hit on a real stranger than a real result —
Apollo has no way to know the name it's matching against doesn't correspond
to a real employee, and neither do we. Running --live against this dataset
will mostly (and correctly) produce 'no_match' or 'org_mismatch' rows; that
is the expected, correct outcome, not a bug to chase.

To guard against silently laundering a wrong match into a usable email, every
attempt is logged to contact_enrichment_runs (append-only, mirrors
enrichment_runs), and only a result that clears verify_match() — an exact
company-domain match AND Apollo's own email_status == 'verified' — is ever
copied to contacts.resolved_email. Nothing else in the pipeline reads that
column; a NULL resolved_email is the expected value for this dataset and
should fail the send step closed (see design/send_sequence_design.md), not
fall back to guessing an address.

Run: python src/find_contact_email.py --contact-id 42            (dry-run, default, no API call)
     python src/find_contact_email.py --contact-id 42 --live      (real Apollo call, costs 1 credit)
     python src/find_contact_email.py --all-demo                  (dry-run every demo contact)
"""

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import urlparse

from dotenv import load_dotenv

from context_builder import (
    DEMO_BRIEFINGS,
    DEMO_MESSAGES,
    get_account_by_name,
    get_contact_by_name,
    split_name,
)
from db import DB_PATH, add_column_if_missing, get_connection, report_failures, run_batch

load_dotenv()

# stdlib urllib, not requests — this repo runs every script as `python
# src/foo.py`, which puts src/ at the front of sys.path. src/queue.py already
# shadows the stdlib `queue` module for that process, which breaks urllib3
# (requests' dependency) at import time. Two small POST calls don't justify
# adding a dependency that collides with an existing sibling-import module.
APOLLO_PEOPLE_MATCH_URL = "https://api.apollo.io/api/v1/people/match"
APOLLO_ORG_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
API_TIMEOUT_SECONDS = 30.0

# Domains this common in citations are noise, not the company's own site —
# excluded so guess_domain_from_evidence() doesn't hand back a news aggregator.
EVIDENCE_DOMAIN_EXCLUDE = {
    "wikipedia.org", "linkedin.com", "facebook.com", "instagram.com",
    "crunchbase.com", "bloomberg.com", "forbes.com", "twitter.com", "x.com",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migration for contact_enrichment_runs + contacts.resolved_email.

    schema.sql already covers a fresh database; this covers upgrading an
    existing db/easol.db created before this table existed. Safe to call on
    every invocation of this module, same pattern as research.ensure_schema().
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_enrichment_runs (
            contact_enrichment_run_id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(contact_id),
            account_id INTEGER NOT NULL REFERENCES accounts(account_id),
            status TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'apollo',
            request_params TEXT,
            matched_person_id TEXT,
            matched_name TEXT,
            matched_organization_name TEXT,
            matched_organization_domain TEXT,
            email TEXT,
            email_status TEXT,
            match_confidence TEXT,
            credits_used INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_enrichment_lookup "
        "ON contact_enrichment_runs(contact_id, status, completed_at)"
    )
    add_column_if_missing(conn, "contacts", "resolved_email", "TEXT")
    add_column_if_missing(
        conn, "contacts", "resolved_email_run_id",
        "INTEGER REFERENCES contact_enrichment_runs(contact_enrichment_run_id)",
    )
    conn.commit()


def guess_domain_from_evidence(conn: sqlite3.Connection, account_id: int) -> str | None:
    """Free, zero-extra-cost domain guess: most-cited non-aggregator domain
    across this account's existing web-search research_evidence (written by
    enrich_account.py). Company-level lookup, not individual — no
    misattribution risk, unlike a personal-LinkedIn search would carry.
    """
    rows = conn.execute(
        "SELECT source_url FROM research_evidence WHERE account_id = ?", (account_id,)
    ).fetchall()
    domains = [urlparse(r["source_url"]).netloc.replace("www.", "") for r in rows]
    domains = [d for d in domains if d and not any(ex in d for ex in EVIDENCE_DOMAIN_EXCLUDE)]
    if not domains:
        return None
    return Counter(domains).most_common(1)[0][0]


def _apollo_post(url: str, api_key: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apollo API error {e.code} calling {url}: {body}") from e


def resolve_company_domain_via_apollo(account_name: str) -> str | None:
    """Fallback when no research evidence exists yet for this account. Still
    company-level (org name -> org domain), so carries none of the
    individual-research risk this module otherwise guards against.
    """
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY not set — copy .env.example to .env and fill it in")
    data = _apollo_post(
        APOLLO_ORG_SEARCH_URL, api_key,
        {"q_organization_name": account_name, "page": 1, "per_page": 1},
    )
    orgs = data.get("organizations", [])
    return orgs[0]["primary_domain"] if orgs else None


def build_request_params(contact: dict, account: dict, domain: str | None) -> dict:
    """Name + company name is Apollo's baseline ('medium' confidence alone per
    Apollo's own guidance); domain is a 'strong' signal layered on top —
    'best combo' is name + org + domain together. Deliberately excludes any
    personal identifier (e.g. a person's LinkedIn URL) that would first
    require an individual-level search to obtain — see module docstring.
    """
    first_name, last_name = split_name(contact["contact_name"])
    params = {
        "first_name": first_name,
        "last_name": last_name,
        "organization_name": account["account_name"],
    }
    if domain:
        params["domain"] = domain
    return params


def call_apollo_people_match(params: dict) -> dict:
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY not set — copy .env.example to .env and fill it in")
    return _apollo_post(APOLLO_PEOPLE_MATCH_URL, api_key, params)


def verify_match(person: dict | None, account_name: str, expected_domain: str | None) -> str:
    """The actual point of this module: a 200 response with a person object is
    NOT proof of a correct match — it is proof Apollo found *some* person
    matching sparse name+org signals. Refuse to trust anything short of an
    exact domain match (when we have one) plus Apollo's own verified status.
    """
    if not person or not person.get("email"):
        return "no_match"
    org = person.get("organization") or {}
    if expected_domain:
        if org.get("primary_domain") != expected_domain:
            return "org_mismatch"
    elif account_name.lower() not in (org.get("name") or "").lower():
        return "org_mismatch"
    if person.get("email_status") != "verified":
        return "unverified"
    return "verified"


def enrich_contact_email(conn: sqlite3.Connection, contact_id: int, *, live: bool = False) -> int:
    ensure_schema(conn)

    contact_row = conn.execute("SELECT * FROM contacts WHERE contact_id = ?", (contact_id,)).fetchone()
    if contact_row is None:
        raise ValueError(f"no contact with id {contact_id}")
    contact = dict(contact_row)
    account = dict(conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (contact["account_id"],)
    ).fetchone())

    domain = guess_domain_from_evidence(conn, account["account_id"])
    if domain is None and live:
        domain = resolve_company_domain_via_apollo(account["account_name"])

    params = build_request_params(contact, account, domain)
    cursor = conn.execute(
        """
        INSERT INTO contact_enrichment_runs (contact_id, account_id, status, provider, request_params)
        VALUES (?, ?, 'running', 'apollo', ?)
        """,
        (contact_id, account["account_id"], json.dumps(params)),
    )
    conn.commit()
    run_id = cursor.lastrowid

    if not live:
        # Dry run: proves the request shape without spending a credit or
        # risking a name-collision misattribution against a synthetic
        # contact. This is the default for a reason — see module docstring.
        conn.execute(
            """
            UPDATE contact_enrichment_runs
            SET status = 'completed', match_confidence = 'dry_run',
                completed_at = CURRENT_TIMESTAMP, error = 'dry_run: no API call made'
            WHERE contact_enrichment_run_id = ?
            """,
            (run_id,),
        )
        conn.commit()
        print(f"[dry-run] contact_id={contact_id} would call Apollo with: {params}")
        return run_id

    try:
        data = call_apollo_people_match(params)
        person = data.get("person")
        confidence = verify_match(person, account["account_name"], domain)
    except Exception as e:
        conn.execute(
            "UPDATE contact_enrichment_runs SET status = 'failed', error = ?, "
            "completed_at = CURRENT_TIMESTAMP WHERE contact_enrichment_run_id = ?",
            (str(e), run_id),
        )
        conn.commit()
        raise

    org = (person or {}).get("organization") or {}
    conn.execute(
        """
        UPDATE contact_enrichment_runs
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
            matched_person_id = ?, matched_name = ?, matched_organization_name = ?,
            matched_organization_domain = ?, email = ?, email_status = ?,
            match_confidence = ?, credits_used = 1
        WHERE contact_enrichment_run_id = ?
        """,
        (
            (person or {}).get("id"),
            (person or {}).get("name"),
            org.get("name"),
            org.get("primary_domain"),
            (person or {}).get("email"),
            (person or {}).get("email_status"),
            confidence,
            run_id,
        ),
    )

    # Only a verified, domain-matched result is ever surfaced as usable —
    # everything else stays quarantined in contact_enrichment_runs for audit.
    if confidence == "verified":
        conn.execute(
            "UPDATE contacts SET resolved_email = ?, resolved_email_run_id = ? WHERE contact_id = ?",
            (person["email"], run_id, contact_id),
        )
    conn.commit()

    print(f"contact_id={contact_id}: match_confidence={confidence} (run_id={run_id})")
    return run_id


def _all_demo_contact_ids(conn: sqlite3.Connection) -> list[int]:
    """Every contact used by either demo sample, deduped, first-seen order —
    same convention as context_builder.demo_account_names().
    """
    seen: set[int] = set()
    ordered: list[int] = []

    for account_name, contact_name in DEMO_MESSAGES:
        account = get_account_by_name(conn, account_name)
        contact = get_contact_by_name(conn, account["account_id"], contact_name)
        if contact["contact_id"] not in seen:
            seen.add(contact["contact_id"])
            ordered.append(contact["contact_id"])

    for account_name in DEMO_BRIEFINGS:
        account = get_account_by_name(conn, account_name)
        for contact in conn.execute(
            "SELECT contact_id FROM contacts WHERE account_id = ?", (account["account_id"],)
        ).fetchall():
            if contact["contact_id"] not in seen:
                seen.add(contact["contact_id"])
                ordered.append(contact["contact_id"])

    return ordered


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Look up a contact's email via Apollo (template — see module docstring before using --live)"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--contact-id", type=int, help="Single contact_id to look up")
    target.add_argument(
        "--all-demo", action="store_true",
        help="Look up every contact used by the demo message/briefing samples",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Make a real Apollo API call (costs credits). Default is --dry-run: "
             "builds and prints the request without calling Apollo.",
    )
    args = parser.parse_args()

    conn = get_connection(DB_PATH)

    contact_ids = [args.contact_id] if args.contact_id is not None else _all_demo_contact_ids(conn)

    _, failures = run_batch(
        contact_ids,
        lambda contact_id: enrich_contact_email(conn, contact_id, live=args.live),
        item_name=lambda contact_id: f"contact_id={contact_id}",
    )

    conn.close()
    report_failures(failures)


if __name__ == "__main__":
    main()
