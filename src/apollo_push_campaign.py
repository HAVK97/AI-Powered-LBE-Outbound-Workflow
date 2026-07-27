"""Stage 8 prep — push approved campaign content into Apollo (template).

This module ADDS the reviewed campaign's subject/bodies onto an Apollo contact
as custom fields so a shared Apollo Sequence template (built once in the Apollo
UI) can merge them. It does NOT enroll the contact, activate the sequence, or
send email — enrollment / send is done in the Apollo platform or via MCP.

Pattern (Apollo-documented):
  - One shared sequence in Apollo UI with steps whose bodies are merge tokens
    for contact custom fields (multi-line text).
  - Touch 1 subject = custom field; touches 2–4 use Apollo's "Reply to previous
    thread" so they inherit the subject (5 fields total, not 8).
  - This script writes those field values for one approved draft's contact.

Preconditions (fail closed):
  - drafts.status in (approved, approved_with_edits) — all touches decided
  - campaign_ready_to_push() true
  - contacts.resolved_email set (find_contact_email.py --live verified match)
  - contact not in suppressions

Default is --dry-run (prints payload, writes apollo_push_runs status=dry_run).
--live calls Apollo Create/Update Contact + PATCH typed_custom_fields.

Run: python src/apollo_push_campaign.py --draft-id 12
     python src/apollo_push_campaign.py --draft-id 12 --live
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

from context_builder import split_name
from db import DB_PATH, get_connection
from find_contact_email import ensure_schema as ensure_contact_email_schema
from queue import (
    campaign_ready_to_push,
    ensure_schema,
    resolve_campaign_content,
)

load_dotenv()

BASE_URL = "https://api.apollo.io/api/v1"
API_TIMEOUT_SECONDS = 30.0

# Custom field IDs from Apollo → Get a List of All Custom Fields / Fields API.
# Create once in Apollo UI as multi-line text (bodies) + single/multi-line (subject).
# Sequence step templates reference them as dynamic variables.
FIELD_ENV_KEYS = {
    "touch_1_subject": "APOLLO_FIELD_TOUCH_1_SUBJECT",
    "touch_1_body": "APOLLO_FIELD_TOUCH_1_BODY",
    "touch_2_body": "APOLLO_FIELD_TOUCH_2_BODY",
    "touch_3_body": "APOLLO_FIELD_TOUCH_3_BODY",
    "touch_4_body": "APOLLO_FIELD_TOUCH_4_BODY",
}


def _master_api_key() -> str:
    key = os.environ.get("APOLLO_MASTER_API_KEY") or os.environ.get("APOLLO_API_KEY")
    if not key:
        raise RuntimeError(
            "APOLLO_MASTER_API_KEY (or APOLLO_API_KEY) not set — "
            "copy .env.example to .env and fill it in"
        )
    return key


def _custom_field_ids() -> dict[str, str]:
    missing = [env for env in FIELD_ENV_KEYS.values() if not os.environ.get(env)]
    if missing:
        raise RuntimeError(
            "Missing Apollo custom field env vars: " + ", ".join(missing) + ". "
            "Create multi-line custom fields in Apollo, then set their IDs in .env "
            "(see design/apollo_campaign_push.md)."
        )
    return {name: os.environ[env] for name, env in FIELD_ENV_KEYS.items()}


def _apollo_request(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-Api-Key": _master_api_key(),
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Apollo API error {e.code} {method} {url}: {err_body}") from e


def build_apollo_payload(conn: sqlite3.Connection, draft_id: int) -> dict:
    """Build the contact + custom-field payload. No network I/O."""
    ensure_schema(conn)
    ensure_contact_email_schema(conn)
    draft = conn.execute(
        """
        SELECT d.*, c.contact_name, c.resolved_email, c.contact_title,
               a.account_name
        FROM drafts d
        JOIN contacts c ON c.contact_id = d.contact_id
        JOIN accounts a ON a.account_id = d.account_id
        WHERE d.draft_id = ?
        """,
        (draft_id,),
    ).fetchone()
    if draft is None:
        raise ValueError(f"no draft with id {draft_id}")
    if draft["draft_type"] != "outreach_campaign":
        raise ValueError(f"draft {draft_id} is {draft['draft_type']!r}, expected outreach_campaign")
    if draft["status"] not in ("approved", "approved_with_edits"):
        raise ValueError(
            f"draft {draft_id} status={draft['status']!r} — approve all touches first "
            f"(python src/review.py)"
        )
    if not campaign_ready_to_push(conn, draft_id):
        raise ValueError(
            f"draft {draft_id} has touches that are not all approved — cannot push"
        )
    if conn.execute(
        "SELECT 1 FROM suppressions WHERE contact_id = ?", (draft["contact_id"],)
    ).fetchone():
        raise ValueError(f"contact {draft['contact_id']} is suppressed — cannot push")
    if not draft["resolved_email"]:
        raise ValueError(
            f"contact {draft['contact_id']} has no resolved_email — run "
            f'python src/find_contact_email.py --contact-id {draft["contact_id"]} '
            f"(--live only against real contacts)"
        )

    content = resolve_campaign_content(conn, draft_id)
    touches_by_step = {t["step"]: t for t in content["touches"]}
    for step in (1, 2, 3, 4):
        if step not in touches_by_step:
            raise ValueError(f"draft {draft_id} missing touch {step}")

    field_ids = _custom_field_ids()
    typed_custom_fields = {
        field_ids["touch_1_subject"]: touches_by_step[1]["subject"],
        field_ids["touch_1_body"]: touches_by_step[1]["body"],
        field_ids["touch_2_body"]: touches_by_step[2]["body"],
        field_ids["touch_3_body"]: touches_by_step[3]["body"],
        field_ids["touch_4_body"]: touches_by_step[4]["body"],
    }

    first_name, last_name = split_name(draft["contact_name"])
    return {
        "draft_id": draft_id,
        "contact_id": draft["contact_id"],
        "account_name": draft["account_name"],
        "contact_name": draft["contact_name"],
        "create_contact": {
            "first_name": first_name,
            "last_name": last_name,
            "email": draft["resolved_email"],
            "title": draft["contact_title"],
            "organization_name": draft["account_name"],
            "run_dedupe": True,
        },
        "typed_custom_fields": typed_custom_fields,
        "touches_preview": [
            {
                "step": t["step"],
                "delay_days": t["delay_days"],
                "subject": t["subject"],
                "body": t["body"],
                "status": t.get("status"),
            }
            for t in content["touches"]
        ],
        "enrollment_note": (
            "This push does NOT enroll or send. In Apollo UI / MCP: add this contact "
            "to the shared sequence (APOLLO_SEQUENCE_ID), with steps 2–4 set to "
            "'Reply to previous thread', bodies = custom-field merge tokens."
        ),
        "apollo_sequence_id": os.environ.get("APOLLO_SEQUENCE_ID"),
    }


def _log_push_run(
    conn: sqlite3.Connection,
    draft_id: int,
    contact_id: int,
    status: str,
    payload: dict,
    *,
    apollo_contact_id: str | None = None,
    error: str | None = None,
) -> int:
    ensure_schema(conn)
    cursor = conn.execute(
        """
        INSERT INTO apollo_push_runs (
            draft_id, contact_id, status, apollo_contact_id, payload, error
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (draft_id, contact_id, status, apollo_contact_id, json.dumps(payload, indent=2), error),
    )
    conn.commit()
    return cursor.lastrowid


def push_campaign(conn: sqlite3.Connection, draft_id: int, *, live: bool = False) -> dict:
    payload = build_apollo_payload(conn, draft_id)

    if not live:
        run_id = _log_push_run(
            conn, draft_id, payload["contact_id"], "dry_run", payload
        )
        print(f"[dry-run] apollo_push_run_id={run_id}")
        print(json.dumps(payload, indent=2))
        print(
            "\nNo API call made. Re-run with --live to create/update the Apollo "
            "contact and write custom fields. Then enroll in Apollo UI or MCP."
        )
        return payload

    # Create (or dedupe) contact, then PATCH custom fields.
    created = _apollo_request("POST", f"{BASE_URL}/contacts", payload["create_contact"])
    person = created.get("contact") or created.get("person") or created
    apollo_contact_id = person.get("id")
    if not apollo_contact_id:
        raise RuntimeError(f"Apollo create contact returned no id: {created}")

    _apollo_request(
        "PATCH",
        f"{BASE_URL}/contacts/{apollo_contact_id}",
        {"typed_custom_fields": payload["typed_custom_fields"]},
    )

    payload["apollo_contact_id"] = apollo_contact_id
    run_id = _log_push_run(
        conn, draft_id, payload["contact_id"], "pushed", payload,
        apollo_contact_id=apollo_contact_id,
    )
    print(f"Pushed draft_id={draft_id} → apollo_contact_id={apollo_contact_id} (run_id={run_id})")
    print(payload["enrollment_note"])
    if payload.get("apollo_sequence_id"):
        print(
            f"Shared sequence id (enroll outside this repo): {payload['apollo_sequence_id']}"
        )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Push approved campaign content onto an Apollo contact's custom fields. "
            "Does not enroll or send — use Apollo UI / MCP for that."
        )
    )
    parser.add_argument("--draft-id", type=int, required=True)
    parser.add_argument(
        "--live", action="store_true",
        help="Call Apollo API (default: dry-run — print payload only)",
    )
    args = parser.parse_args()

    # Field IDs are required even for dry-run so the printed payload matches prod shape.
    # Allow dry-run without them by substituting placeholders.
    if not args.live:
        for env in FIELD_ENV_KEYS.values():
            os.environ.setdefault(env, f"PLACEHOLDER_{env}")

    conn = get_connection(DB_PATH)

    try:
        push_campaign(conn, args.draft_id, live=args.live)
    except Exception as e:
        ensure_schema(conn)
        try:
            draft = conn.execute(
                "SELECT contact_id FROM drafts WHERE draft_id = ?", (args.draft_id,)
            ).fetchone()
            contact_id = draft["contact_id"] if draft else None
        except Exception:
            contact_id = None
        _log_push_run(
            conn, args.draft_id, contact_id, "failed",
            {"draft_id": args.draft_id}, error=str(e),
        )
        print(f"FAILED: {e}", flush=True)
        conn.close()
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
