"""Task B — review CLI (PRD §7). The only human gate in this system: nothing
moves out of pending_review without an explicit approve / approve-with-edits /
reject decision here.

Outreach campaigns are reviewed per-touch (all 4 shown for narrative context;
decisions recorded on draft_touches). Briefings keep whole-draft review.

Launch / Apollo enrollment is NOT triggered from this script — after all touches
are approved, use Apollo UI / MCP (or optionally python src/apollo_push_campaign.py
to push content onto the Apollo contact's custom fields).

Run: python src/review.py   (from repo root)
"""

import sqlite3

from db import DB_PATH, get_connection
from generate_message import format_draft_content
from queue import (
    CAMPAIGN_DRAFT_TYPES,
    REASON_CODES,
    apply_touch_transition,
    apply_transition,
    backfill_touches_from_content,
    ensure_schema,
    get_touches,
    list_pending,
)

ACTOR = "self_review"


def prompt_reason_code() -> str:
    print("Select a reason code:")
    for i, code in enumerate(REASON_CODES, start=1):
        print(f"  {i}. {code}")
    while True:
        choice = input(f"Reason code [1-{len(REASON_CODES)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(REASON_CODES):
            return REASON_CODES[int(choice) - 1]
        print("Invalid choice, try again.")


def _read_multiline(prompt: str) -> str:
    print(f"{prompt} (end with a line containing only END)")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def _prompt_action() -> str:
    while True:
        action = input("Approve (a) / Edit (e) / Reject (r) / Skip (s): ").strip().lower()
        if action in ("a", "e", "r", "s"):
            return action
        print("Please enter a, e, r, or s.")


def _apply_touch_decision(
    conn: sqlite3.Connection,
    touch: dict,
    to_status: str,
    *,
    edited_subject: str | None = None,
    edited_body: str | None = None,
) -> None:
    if to_status == "rejected":
        print("Rejecting — a reason is required.")
        reason_code = prompt_reason_code()
        note = input("Optional note (press enter to skip): ").strip() or None
        apply_touch_transition(
            conn, touch["draft_touch_id"], "rejected", ACTOR, rating=0,
            reason_code=reason_code, quality_note=note,
        )
        print(f"Touch {touch['step']} rejected.")
        return

    apply_touch_transition(
        conn, touch["draft_touch_id"], to_status, ACTOR, rating=1,
        edited_subject=edited_subject, edited_body=edited_body,
    )
    print(f"Touch {touch['step']}: {to_status}.")


def _show_campaign_context(draft: dict, touches: list[dict]) -> None:
    print("\n" + "=" * 70)
    print(
        f"draft_id={draft['draft_id']}  [{draft['draft_type']}]  "
        f"{draft['account_name']} -> {draft['contact_name']}"
    )
    print(f"value_prop_used={draft['value_prop_used']}  reason={draft['value_prop_reason']}")
    print("-" * 70)
    print("Full campaign (context — decide per touch below):\n")
    for t in touches:
        marker = f"[{t['status']}]"
        print(f"### Touch {t['step']} (day {t['delay_days']}) {marker}")
        print(f"Subject: {t['current_subject']}\n")
        print(t["current_body"])
        print()
    print("=" * 70)


def review_campaign(conn: sqlite3.Connection, draft: dict) -> None:
    touches = backfill_touches_from_content(conn, draft["draft_id"])
    if not touches:
        print(
            f"draft_id={draft['draft_id']}: campaign has no parseable touches — "
            "falling back to whole-draft review."
        )
        review_whole_draft(conn, draft)
        return

    _show_campaign_context(draft, touches)
    pending = [t for t in touches if t["status"] == "pending_review"]
    if not pending:
        print("No touches pending review for this campaign.")
        return

    print(f"\n{len(pending)} touch(es) pending review. Decide each step individually.\n")
    for touch in pending:
        # re-fetch in case of concurrent changes (safe if extended later)
        current = conn.execute(
            "SELECT * FROM draft_touches WHERE draft_touch_id = ?",
            (touch["draft_touch_id"],),
        ).fetchone()
        if current is None or current["status"] != "pending_review":
            continue
        touch = dict(current)

        print(f"\n--- Reviewing Touch {touch['step']} (day {touch['delay_days']}) ---")
        print(f"Subject: {touch['current_subject']}\n")
        print(touch["current_body"])
        print("-" * 40)

        action = _prompt_action()
        if action == "s":
            print(f"Skipped touch {touch['step']}.")
            continue
        if action == "r":
            _apply_touch_decision(conn, touch, "rejected")
            continue
        if action == "a":
            _apply_touch_decision(conn, touch, "approved")
            continue

        # edit
        print("Enter edited subject (single line):")
        edited_subject = input().strip()
        if not edited_subject:
            print("Empty subject — skipping this touch.")
            continue
        edited_body = _read_multiline("Enter edited body")
        if not edited_body.strip():
            print("Empty body — skipping this touch.")
            continue
        _apply_touch_decision(
            conn, touch, "approved_with_edits",
            edited_subject=edited_subject, edited_body=edited_body,
        )

    final = conn.execute(
        "SELECT status FROM drafts WHERE draft_id = ?", (draft["draft_id"],)
    ).fetchone()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM draft_touches WHERE draft_id = ? AND status = 'pending_review'",
        (draft["draft_id"],),
    ).fetchone()[0]
    if remaining == 0:
        print(
            f"\nCampaign draft_id={draft['draft_id']} rollup status: {final['status']}. "
            "To push content into Apollo (not enroll/send), run:\n"
            f"  python src/apollo_push_campaign.py --draft-id {draft['draft_id']}\n"
            "Enrollment / activation stays in Apollo UI or MCP."
        )
    else:
        print(f"\n{remaining} touch(es) still pending on draft_id={draft['draft_id']}.")


def review_whole_draft(conn: sqlite3.Connection, draft: dict) -> None:
    """Briefings and legacy drafts without per-touch rows."""
    print("\n" + "=" * 70)
    label = draft["contact_name"] or "(briefing — all contacts)"
    print(f"draft_id={draft['draft_id']}  [{draft['draft_type']}]  {draft['account_name']} -> {label}")
    if draft["draft_type"] in CAMPAIGN_DRAFT_TYPES:
        print(f"value_prop_used={draft['value_prop_used']}  reason={draft['value_prop_reason']}")
    print("-" * 70)
    print(format_draft_content(draft["content"], draft["draft_type"]))
    print("=" * 70)

    action = _prompt_action()
    if action == "s":
        print("Skipped.")
        return
    if action == "r":
        print("Rejecting — a reason is required.")
        reason_code = prompt_reason_code()
        note = input("Optional note (press enter to skip): ").strip() or None
        apply_transition(
            conn, draft["draft_id"], "rejected", ACTOR, rating=0,
            reason_code=reason_code, quality_note=note,
        )
        print("Rejected.")
        return

    edited_content = None
    to_status = "approved"
    if action == "e":
        edited_content = _read_multiline("Enter the edited content")
        to_status = "approved_with_edits"

    apply_transition(
        conn, draft["draft_id"], to_status, ACTOR, rating=1, edited_content=edited_content
    )
    print(f"{to_status}.")


def review_one(conn: sqlite3.Connection, draft: dict) -> None:
    if draft["draft_type"] == "outreach_campaign":
        review_campaign(conn, draft)
    else:
        review_whole_draft(conn, draft)


def main() -> None:
    conn = get_connection(DB_PATH)
    ensure_schema(conn)

    pending = list_pending(conn)
    if not pending:
        print("No drafts pending review.")
        conn.close()
        return

    print(f"{len(pending)} draft(s) pending review.\n")
    for draft in pending:
        current = conn.execute(
            "SELECT status FROM drafts WHERE draft_id = ?", (draft["draft_id"],)
        ).fetchone()
        if current["status"] != "pending_review":
            continue
        # campaigns with remaining pending touches stay pending_review until rollup
        if draft["draft_type"] == "outreach_campaign":
            touches = get_touches(conn, draft["draft_id"]) or backfill_touches_from_content(
                conn, draft["draft_id"]
            )
            if touches and not any(t["status"] == "pending_review" for t in touches):
                continue
        review_one(conn, draft)

    conn.close()
    print("\nReview session complete.")


if __name__ == "__main__":
    main()
