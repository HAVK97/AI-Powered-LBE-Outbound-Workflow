"""Queue & status-transition logic (PRD §4, §7).

review.py is the only caller that should mutate drafts.status / draft_touches.status,
and only through apply_transition() / apply_touch_transition() — never by writing
status columns directly. draft_events is append-only.

For outreach_campaign drafts, each of the 4 touches is reviewed individually
(draft_touches) while the full campaign is shown for narrative context. drafts.status
is a rollup once every touch has a decision.
"""

import json
import sqlite3

from db import add_column_if_missing

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"approved", "approved_with_edits", "rejected"},
    "approved": {"queued"},
    "approved_with_edits": {"queued"},
    "queued": {"sent"},
    "rejected": set(),
    "sent": set(),
}

REASON_CODES: list[str] = [
    "generic",
    "incorrect_claim",
    "weak_value_case",
    "wrong_role_angle",
    "poor_tone",
    "too_long",
    "compliance_risk",
    "other",
]

CAMPAIGN_DRAFT_TYPES = frozenset({"outreach_campaign", "outreach_message"})


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migration for draft_touches, draft_events.draft_touch_id, apollo_push_runs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_touches (
            draft_touch_id INTEGER PRIMARY KEY,
            draft_id INTEGER NOT NULL REFERENCES drafts(draft_id),
            step INTEGER NOT NULL,
            delay_days INTEGER NOT NULL,
            channel TEXT NOT NULL,
            ai_subject TEXT NOT NULL,
            ai_body TEXT NOT NULL,
            current_subject TEXT NOT NULL,
            current_body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review',
            UNIQUE (draft_id, step)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_draft_touches_draft ON draft_touches(draft_id, status)"
    )
    add_column_if_missing(
        conn, "draft_events", "draft_touch_id",
        "INTEGER REFERENCES draft_touches(draft_touch_id)",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS apollo_push_runs (
            apollo_push_run_id INTEGER PRIMARY KEY,
            draft_id INTEGER NOT NULL REFERENCES drafts(draft_id),
            contact_id INTEGER REFERENCES contacts(contact_id),
            status TEXT NOT NULL,
            apollo_contact_id TEXT,
            payload TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def list_pending(conn: sqlite3.Connection) -> list[dict]:
    """Drafts still awaiting human review.

    Campaigns: pending_review drafts (touches may still be open).
    Briefings / legacy: pending_review with no touch rows.
    """
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT d.*, a.account_name, c.contact_name, c.contact_category
        FROM drafts d
        JOIN accounts a ON a.account_id = d.account_id
        LEFT JOIN contacts c ON c.contact_id = d.contact_id
        WHERE d.status = 'pending_review'
        ORDER BY d.draft_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_touches(conn: sqlite3.Connection, draft_id: int) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM draft_touches WHERE draft_id = ? ORDER BY step", (draft_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def insert_campaign_touches(conn: sqlite3.Connection, draft_id: int, touches: list[dict]) -> None:
    """Write the 4 per-touch rows after campaign generation. Idempotent per draft_id."""
    ensure_schema(conn)
    existing = conn.execute(
        "SELECT COUNT(*) FROM draft_touches WHERE draft_id = ?", (draft_id,)
    ).fetchone()[0]
    if existing:
        return
    for touch in sorted(touches, key=lambda t: t["step"]):
        conn.execute(
            """
            INSERT INTO draft_touches (
                draft_id, step, delay_days, channel,
                ai_subject, ai_body, current_subject, current_body, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')
            """,
            (
                draft_id,
                touch["step"],
                touch["delay_days"],
                touch["channel"],
                touch["subject"],
                touch["body"],
                touch["subject"],
                touch["body"],
            ),
        )
    conn.commit()


def backfill_touches_from_content(conn: sqlite3.Connection, draft_id: int) -> list[dict]:
    """For legacy campaign drafts generated before draft_touches existed."""
    ensure_schema(conn)
    existing = get_touches(conn, draft_id)
    if existing:
        return existing
    row = conn.execute(
        "SELECT content, draft_type FROM drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    if row is None or row["draft_type"] not in CAMPAIGN_DRAFT_TYPES:
        return []
    try:
        parsed = json.loads(row["content"])
    except (TypeError, json.JSONDecodeError):
        return []
    touches = parsed.get("touches")
    if not isinstance(touches, list) or len(touches) != 4:
        return []
    insert_campaign_touches(conn, draft_id, touches)
    return get_touches(conn, draft_id)


def campaign_ready_to_push(conn: sqlite3.Connection, draft_id: int) -> bool:
    """True when every touch is approved / approved_with_edits (none pending/rejected)."""
    touches = get_touches(conn, draft_id)
    if not touches:
        return False
    return all(t["status"] in ("approved", "approved_with_edits") for t in touches)


def resolve_campaign_content(conn: sqlite3.Connection, draft_id: int) -> dict:
    """Current campaign JSON: live subject/body per touch from draft_touches.

    Falls back to drafts.content (AI original) when no touch rows exist.
    value_prop_* always come from the drafts row / original content.
    """
    ensure_schema(conn)
    draft = conn.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    if draft is None:
        raise ValueError(f"no draft with id {draft_id}")

    touches = get_touches(conn, draft_id)
    if touches:
        return {
            "value_prop_used": draft["value_prop_used"],
            "value_prop_reason": draft["value_prop_reason"],
            "touches": [
                {
                    "step": t["step"],
                    "delay_days": t["delay_days"],
                    "channel": t["channel"],
                    "subject": t["current_subject"],
                    "body": t["current_body"],
                    "status": t["status"],
                }
                for t in touches
            ],
        }

    # legacy: whole-draft edit in draft_events, else original content
    edit = conn.execute(
        """
        SELECT edited_content FROM draft_events
        WHERE draft_id = ? AND draft_touch_id IS NULL AND edited_content IS NOT NULL
        ORDER BY created_at DESC, event_id DESC LIMIT 1
        """,
        (draft_id,),
    ).fetchone()
    if edit:
        return json.loads(edit["edited_content"])
    return json.loads(draft["content"])


def _validate_rating_and_reason(rating: int, reason_code: str | None) -> None:
    if rating not in (0, 1):
        raise ValueError(f"rating must be 0 or 1, got {rating!r}")
    if rating == 0 and not reason_code:
        raise ValueError("reason_code is required when rating = 0")
    if reason_code is not None and reason_code not in REASON_CODES:
        raise ValueError(f"invalid reason_code: {reason_code!r} (must be one of {REASON_CODES})")


def apply_transition(
    conn: sqlite3.Connection,
    draft_id: int,
    to_status: str,
    actor: str,
    rating: int,
    reason_code: str | None = None,
    quality_note: str | None = None,
    edited_content: str | None = None,
) -> None:
    """Whole-draft transition (briefings, rollup, legacy). Never write drafts.status directly."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT status, contact_id, account_id FROM drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no draft with id {draft_id}")
    from_status, contact_id, account_id = row

    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise ValueError(
            f"illegal transition: {from_status!r} -> {to_status!r} (allowed: {sorted(allowed)})"
        )

    _validate_rating_and_reason(rating, reason_code)
    if to_status == "approved_with_edits" and not edited_content:
        raise ValueError("edited_content is required when to_status = approved_with_edits")

    conn.execute(
        """
        INSERT INTO draft_events (
            draft_id, draft_touch_id, from_status, to_status, actor, rating,
            reason_code, quality_note, edited_content
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (draft_id, from_status, to_status, actor, rating, reason_code, quality_note, edited_content),
    )
    conn.execute("UPDATE drafts SET status = ? WHERE draft_id = ?", (to_status, draft_id))

    if reason_code == "compliance_risk" and contact_id is not None:
        conn.execute(
            """
            INSERT INTO suppressions (contact_id, account_id, reason, source)
            VALUES (?, ?, ?, 'review.py')
            """,
            (contact_id, account_id, f"compliance_risk flagged on draft {draft_id} review"),
        )

    conn.commit()


def _rollup_campaign_status(conn: sqlite3.Connection, draft_id: int, actor: str) -> None:
    """When every touch is decided, set drafts.status to the aggregate outcome."""
    touches = get_touches(conn, draft_id)
    if not touches or any(t["status"] == "pending_review" for t in touches):
        return

    statuses = {t["status"] for t in touches}
    if "rejected" in statuses:
        to_status = "rejected"
    elif "approved_with_edits" in statuses:
        to_status = "approved_with_edits"
    else:
        to_status = "approved"

    draft = conn.execute("SELECT status FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    if draft is None or draft["status"] == to_status:
        return
    if draft["status"] != "pending_review":
        return

    # Rollup event — rating=1 for approve paths; rejected rollup uses rating=0
    # with reason_code reflecting the aggregate (individual touch events already
    # carry the real per-touch reasons).
    rating = 0 if to_status == "rejected" else 1
    reason_code = "other" if to_status == "rejected" else None
    conn.execute(
        """
        INSERT INTO draft_events (
            draft_id, draft_touch_id, from_status, to_status, actor, rating,
            reason_code, quality_note, edited_content
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            draft_id,
            draft["status"],
            to_status,
            actor,
            rating,
            reason_code,
            f"rollup: all touches decided → {to_status}",
        ),
    )
    conn.execute("UPDATE drafts SET status = ? WHERE draft_id = ?", (to_status, draft_id))
    conn.commit()


def apply_touch_transition(
    conn: sqlite3.Connection,
    draft_touch_id: int,
    to_status: str,
    actor: str,
    rating: int,
    reason_code: str | None = None,
    quality_note: str | None = None,
    edited_subject: str | None = None,
    edited_body: str | None = None,
) -> None:
    """Per-touch approve / approve-with-edits / reject. Updates current_* on edit."""
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT t.status, t.draft_id, d.contact_id, d.account_id
        FROM draft_touches t
        JOIN drafts d ON d.draft_id = t.draft_id
        WHERE t.draft_touch_id = ?
        """,
        (draft_touch_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no draft_touch with id {draft_touch_id}")
    from_status, draft_id, contact_id, account_id = row

    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    # touches only use the review outcomes, not queued/sent
    review_targets = {"approved", "approved_with_edits", "rejected"}
    if to_status not in allowed or to_status not in review_targets:
        raise ValueError(
            f"illegal touch transition: {from_status!r} -> {to_status!r} "
            f"(allowed: {sorted(allowed & review_targets)})"
        )

    _validate_rating_and_reason(rating, reason_code)
    if to_status == "approved_with_edits" and not (edited_subject and edited_body):
        raise ValueError(
            "edited_subject and edited_body are required when to_status = approved_with_edits"
        )

    edited_payload = None
    if to_status == "approved_with_edits":
        edited_payload = json.dumps({"subject": edited_subject, "body": edited_body})

    conn.execute(
        """
        INSERT INTO draft_events (
            draft_id, draft_touch_id, from_status, to_status, actor, rating,
            reason_code, quality_note, edited_content
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id, draft_touch_id, from_status, to_status, actor, rating,
            reason_code, quality_note, edited_payload,
        ),
    )
    conn.execute(
        "UPDATE draft_touches SET status = ? WHERE draft_touch_id = ?",
        (to_status, draft_touch_id),
    )
    if to_status == "approved_with_edits":
        conn.execute(
            """
            UPDATE draft_touches
            SET current_subject = ?, current_body = ?
            WHERE draft_touch_id = ?
            """,
            (edited_subject, edited_body, draft_touch_id),
        )

    if reason_code == "compliance_risk" and contact_id is not None:
        conn.execute(
            """
            INSERT INTO suppressions (contact_id, account_id, reason, source)
            VALUES (?, ?, ?, 'review.py')
            """,
            (
                contact_id,
                account_id,
                f"compliance_risk flagged on draft_touch {draft_touch_id} (draft {draft_id})",
            ),
        )

    conn.commit()
    _rollup_campaign_status(conn, draft_id, actor)
