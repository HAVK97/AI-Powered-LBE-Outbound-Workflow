"""Stage 9 — presentation (PRD §5 Stage 9, §16). Renders a static, read-only
HTML export focused on the demo pipeline: pipeline status, generated content,
research dossiers, review audit trail, and economics. Raw table dumps live in
a collapsible appendix.

Run: python src/dashboard.py   (from repo root)
Review actions: python src/review.py (terminal — not in this export)
"""

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from context_builder import (
    DEMO_BRIEFINGS,
    DEMO_MESSAGES,
    demo_account_names,
    get_account_by_name,
    get_contact_by_name,
)
from db import DB_PATH, REPO_ROOT, get_connection
from generate_message import DRAFT_TYPE, format_draft_content
from queue import backfill_touches_from_content, ensure_schema as ensure_queue_schema, get_touches
from research import ensure_schema

OUTPUT_PATH = REPO_ROOT / "outputs" / "dashboard.html"

DEMO_ASSET_COUNT = len(DEMO_MESSAGES) + len(DEMO_BRIEFINGS)

CSS = """
body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fafafa; max-width: 1200px; }
h1 { margin-bottom: 0.2rem; }
h2 { margin-top: 2.5rem; border-bottom: 2px solid #333; padding-bottom: 0.3rem; }
h3 { margin-top: 0; }
.subtitle { color: #444; font-size: 1rem; margin: 0.3rem 0 0.5rem; }
.meta { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
.nav { margin: 1rem 0 1.5rem; font-size: 0.9rem; }
.nav a { margin-right: 1rem; color: #0366d6; }
.cards { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0 2rem; }
.card { background: white; border: 1px solid #ddd; border-radius: 6px; padding: 1rem 1.5rem; min-width: 140px; }
.card .value { font-size: 1.8rem; font-weight: 700; }
.card .label { font-size: 0.85rem; color: #666; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; background: white; }
th, td { border: 1px solid #ddd; padding: 0.4rem 0.6rem; font-size: 0.85rem; text-align: left; vertical-align: top; }
th { background: #333; color: white; }
tr:nth-child(even) { background: #f5f5f5; }
.badge { display: inline-block; padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; white-space: nowrap; }
.badge-pending_review { background: #fff3cd; color: #856404; }
.badge-approved { background: #d4edda; color: #155724; }
.badge-approved_with_edits { background: #cce5ff; color: #004085; }
.badge-rejected { background: #f8d7da; color: #721c24; }
.badge-completed { background: #d4edda; color: #155724; }
.badge-failed { background: #f8d7da; color: #721c24; }
.badge-none { background: #e9ecef; color: #6c757d; }
.status-pending_review { color: #b8860b; font-weight: 600; }
.status-approved, .status-approved_with_edits { color: #1a7a1a; font-weight: 600; }
.status-rejected { color: #a11; font-weight: 600; }
.rating-1 { color: #1a7a1a; }
.rating-0 { color: #a11; font-weight: 600; background: #fff5f5; }
.note { color: #555; font-size: 0.85rem; }
.content-card { background: white; border: 1px solid #ddd; border-radius: 8px; padding: 1.25rem 1.5rem; margin-bottom: 1.25rem; }
.content-card header { border-bottom: 1px solid #eee; padding-bottom: 0.75rem; margin-bottom: 1rem; }
.touch-block { margin: 1rem 0; padding: 0.75rem 1rem; background: #f8f9fa; border-radius: 6px; border-left: 3px solid #333; }
.touch-block.edited { border-left-color: #0366d6; }
.touch-subject { font-weight: 600; margin-bottom: 0.5rem; }
.touch-body { white-space: pre-wrap; line-height: 1.5; }
.edit-note { color: #0366d6; font-size: 0.8rem; margin-top: 0.4rem; }
.dossier { background: white; border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; }
.dossier ul { margin: 0.5rem 0 0; padding-left: 1.25rem; }
.dossier li { margin-bottom: 0.6rem; line-height: 1.45; }
.brief-content { white-space: pre-wrap; line-height: 1.55; font-size: 0.9rem; }
details { margin-top: 1.5rem; background: white; border: 1px solid #ddd; border-radius: 6px; padding: 0.75rem 1rem; }
details summary { cursor: pointer; font-weight: 600; padding: 0.25rem 0; }
details[open] summary { margin-bottom: 1rem; }
"""


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _badge(status: str | None, *, label: str | None = None) -> str:
    if not status:
        status = "none"
    text = label or status.replace("_", " ")
    css = status if status in (
        "pending_review", "approved", "approved_with_edits", "rejected",
        "completed", "failed",
    ) else "none"
    return f'<span class="badge badge-{css}">{_esc(text)}</span>'


def _action_label(to_status: str) -> str:
    return {
        "approved": "Approve",
        "approved_with_edits": "Edit",
        "rejected": "Reject",
    }.get(to_status, to_status.replace("_", " "))


def _latest_enrichment(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT r.*,
               (SELECT COUNT(*) FROM research_evidence e
                WHERE e.enrichment_run_id = r.enrichment_run_id) AS claim_count
        FROM enrichment_runs r
        WHERE r.account_id = ? AND r.status = 'completed'
        ORDER BY r.completed_at DESC, r.enrichment_run_id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()


def _latest_campaign_draft(
    conn: sqlite3.Connection, account_id: int, contact_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM drafts
        WHERE account_id = ? AND contact_id = ? AND draft_type = ?
        ORDER BY draft_id DESC LIMIT 1
        """,
        (account_id, contact_id, DRAFT_TYPE),
    ).fetchone()


def _latest_briefing_draft(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM drafts
        WHERE account_id = ? AND contact_id IS NULL AND draft_type = 'briefing'
        ORDER BY draft_id DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()


def _enrichment_label(run: sqlite3.Row | None) -> str:
    if run is None:
        return "—"
    date = run["completed_at"] or "?"
    return f'run #{run["enrichment_run_id"]}<br><span class="note">{_esc(date)}</span>'


def _demo_pipeline_table(conn: sqlite3.Connection) -> str:
    campaign_rows = []
    for account_name, contact_name in DEMO_MESSAGES:
        try:
            account = get_account_by_name(conn, account_name)
            contact = get_contact_by_name(conn, account["account_id"], contact_name)
        except ValueError:
            campaign_rows.append(
                f"<tr><td>{_esc(account_name)}</td><td>{_esc(contact_name)}</td>"
                f'<td colspan="9" class="note">Not loaded — run load_data.py</td></tr>'
            )
            continue

        enrichment = _latest_enrichment(conn, account["account_id"])
        draft = _latest_campaign_draft(conn, account["account_id"], contact["contact_id"])

        touch_badges = ["—", "—", "—", "—"]
        draft_status = "—"
        value_prop = "—"
        cost = "—"

        if draft:
            draft_status = _badge(draft["status"])
            value_prop = _esc(draft["value_prop_used"] or "—")
            cost = f'${draft["estimated_cost_usd"]:.4f}'
            touches = get_touches(conn, draft["draft_id"]) or backfill_touches_from_content(
                conn, draft["draft_id"]
            )
            for t in touches:
                idx = t["step"] - 1
                if 0 <= idx < 4:
                    touch_badges[idx] = _badge(t["status"], label=f'T{t["step"]}')

        campaign_rows.append(
            f"<tr><td>{_esc(account_name)}</td><td>{_esc(contact_name)}</td>"
            f"<td>{_esc(contact['contact_category'])}</td>"
            f"<td>{_enrichment_label(enrichment)}</td>"
            f"<td>{value_prop}</td><td>{draft_status}</td>"
            f"{''.join(f'<td>{b}</td>' for b in touch_badges)}"
            f"<td>{cost}</td></tr>"
        )

    briefing_rows = []
    for account_name in DEMO_BRIEFINGS:
        try:
            account = get_account_by_name(conn, account_name)
        except ValueError:
            briefing_rows.append(
                f"<tr><td>{_esc(account_name)}</td><td colspan=\"4\" class=\"note\">Not loaded</td></tr>"
            )
            continue

        enrichment = _latest_enrichment(conn, account["account_id"])
        draft = _latest_briefing_draft(conn, account["account_id"])
        draft_status = _badge(draft["status"]) if draft else "—"
        cost = f'${draft["estimated_cost_usd"]:.4f}' if draft else "—"

        briefing_rows.append(
            f"<tr><td>{_esc(account_name)}</td>"
            f"<td>{_enrichment_label(enrichment)}</td>"
            f"<td>{draft_status}</td><td>{cost}</td></tr>"
        )

    return f"""
<h3>Campaigns (5)</h3>
<table>
<thead><tr>
  <th>Account</th><th>Contact</th><th>Role</th><th>Enriched</th>
  <th>Value prop</th><th>Draft status</th>
  <th>Touch 1</th><th>Touch 2</th><th>Touch 3</th><th>Touch 4</th><th>Cost</th>
</tr></thead>
<tbody>{''.join(campaign_rows)}</tbody>
</table>

<h3>Briefings (2)</h3>
<table>
<thead><tr><th>Account</th><th>Enriched</th><th>Draft status</th><th>Cost</th></tr></thead>
<tbody>{''.join(briefing_rows)}</tbody>
</table>
"""


def _campaign_content_card(conn: sqlite3.Connection, draft: sqlite3.Row, account_name: str, contact_name: str, role: str) -> str:
    touches = get_touches(conn, draft["draft_id"]) or backfill_touches_from_content(
        conn, draft["draft_id"]
    )
    touch_html = []
    if touches:
        for t in touches:
            edited = (
                t["current_subject"] != t["ai_subject"]
                or t["current_body"] != t["ai_body"]
            )
            edit_note = '<p class="edit-note">Edited from AI original</p>' if edited else ""
            touch_html.append(
                f'<div class="touch-block{" edited" if edited else ""}">'
                f'<div>{_badge(t["status"], label=f'Touch {t["step"]} · day {t["delay_days"]}')}</div>'
                f'<p class="touch-subject">Subject: {_esc(t["current_subject"])}</p>'
                f'<div class="touch-body">{_esc(t["current_body"])}</div>'
                f"{edit_note}</div>"
            )
    else:
        touch_html.append(
            f'<pre class="brief-content">{_esc(format_draft_content(draft["content"], draft["draft_type"]))}</pre>'
        )

    return f"""
<article class="content-card" id="draft-{draft['draft_id']}">
  <header>
    <h3>{_esc(account_name)} &rarr; {_esc(contact_name)} <span class="note">({_esc(role)})</span></h3>
    <p class="note">
      draft_id={draft['draft_id']} · {_badge(draft['status'])}
      · value prop: <strong>{_esc(draft['value_prop_used'])}</strong>
      · {_esc(draft['value_prop_reason'] or '')}
      · ${_esc(f'{draft["estimated_cost_usd"]:.4f}')} generation
    </p>
  </header>
  {''.join(touch_html)}
</article>
"""


def _briefing_content_card(conn: sqlite3.Connection, draft: sqlite3.Row, account_name: str) -> str:
    return f"""
<article class="content-card" id="draft-{draft['draft_id']}">
  <header>
    <h3>Briefing: {_esc(account_name)}</h3>
    <p class="note">
      draft_id={draft['draft_id']} · {_badge(draft['status'])}
      · ${_esc(f'{draft["estimated_cost_usd"]:.4f}')} generation
    </p>
  </header>
  <div class="brief-content">{_esc(draft['content'])}</div>
</article>
"""


def _demo_content_cards(conn: sqlite3.Connection) -> str:
    cards = []
    for account_name, contact_name in DEMO_MESSAGES:
        try:
            account = get_account_by_name(conn, account_name)
            contact = get_contact_by_name(conn, account["account_id"], contact_name)
            draft = _latest_campaign_draft(conn, account["account_id"], contact["contact_id"])
        except ValueError:
            continue
        if draft:
            cards.append(
                _campaign_content_card(
                    conn, draft, account_name, contact_name, contact["contact_category"]
                )
            )

    for account_name in DEMO_BRIEFINGS:
        try:
            account = get_account_by_name(conn, account_name)
            draft = _latest_briefing_draft(conn, account["account_id"])
        except ValueError:
            continue
        if draft:
            cards.append(_briefing_content_card(conn, draft, account_name))

    if not cards:
        return '<p class="note">No generated content yet — run generate_message.py and generate_briefing.py.</p>'
    return "".join(cards)


def _demo_research_dossiers(conn: sqlite3.Connection) -> tuple[str, int]:
    sections = []
    claim_total = 0
    for account_name in demo_account_names():
        try:
            account = get_account_by_name(conn, account_name)
        except ValueError:
            continue
        run = _latest_enrichment(conn, account["account_id"])
        if run is None:
            sections.append(
                f'<div class="dossier"><h3>{_esc(account_name)}</h3>'
                f'<p class="note">Not enriched — run enrich_account.py --account "{_esc(account_name)}"</p></div>'
            )
            continue

        claims = conn.execute(
            "SELECT claim, source_url FROM research_evidence "
            "WHERE enrichment_run_id = ? ORDER BY evidence_id",
            (run["enrichment_run_id"],),
        ).fetchall()
        claim_total += len(claims)
        items = []
        for c in claims:
            url = _esc(c["source_url"])
            items.append(
                f"<li>{_esc(c['claim'])}<br>"
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a></li>'
            )
        sections.append(
            f'<div class="dossier"><h3>{_esc(account_name)}</h3>'
            f'<p class="note">enrichment_run_id={run["enrichment_run_id"]} · '
            f'{run["claim_count"]} claim(s) · ${(run["estimated_cost_usd"] or 0):.4f} · '
            f'completed {_esc(run["completed_at"])}</p>'
            f"<ul>{''.join(items)}</ul></div>"
        )
    return "".join(sections), claim_total


def _events_table(conn: sqlite3.Connection) -> tuple[str, int]:
    rows = conn.execute(
        """
        SELECT e.*, a.account_name, d.draft_type, t.step AS touch_step
        FROM draft_events e
        JOIN drafts d ON d.draft_id = e.draft_id
        JOIN accounts a ON a.account_id = d.account_id
        LEFT JOIN draft_touches t ON t.draft_touch_id = e.draft_touch_id
        ORDER BY e.event_id DESC
        """
    ).fetchall()
    body = []
    for r in rows:
        rating_class = f"rating-{r['rating']}"
        rating_label = "up" if r["rating"] == 1 else "down"
        if r["draft_touch_id"]:
            touch_label = f"Touch {r['touch_step']}"
        elif r["to_status"] in ("approved", "approved_with_edits", "rejected") and "rollup" in (r["quality_note"] or ""):
            touch_label = "Rollup"
        else:
            touch_label = "—"
        row_class = ' class="rating-0"' if r["rating"] == 0 else ""
        body.append(
            f"<tr{row_class}><td>{_esc(r['created_at'])}</td>"
            f"<td>{r['draft_id']}</td><td>{_esc(r['account_name'])}</td>"
            f"<td>{_esc(r['draft_type'])}</td><td>{touch_label}</td>"
            f"<td>{_esc(_action_label(r['to_status']))}</td>"
            f"<td class=\"{rating_class}\">{rating_label}</td>"
            f"<td>{_esc(r['reason_code'])}</td>"
            f"<td class=\"note\">{_esc(r['quality_note'])}</td></tr>"
        )
    if not body:
        return '<p class="note">No review events yet — run review.py after generation.</p>', 0
    table = (
        "<table><thead><tr><th>At</th><th>Draft</th><th>Account</th><th>Type</th>"
        "<th>Touch</th><th>Action</th><th>Rating</th><th>Reason</th><th>Note</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )
    return table, len(rows)


def _economics_table(enrichment_cost: float, enrichment_completed: int, generation_cost: float, draft_count: int, approved_count: int) -> str:
    total = enrichment_cost + generation_cost
    per_approved = total / approved_count if approved_count else 0
    return f"""
<table>
<thead><tr><th>Stage</th><th>Runs / drafts</th><th>Cost</th></tr></thead>
<tbody>
  <tr><td>Enrichment (web search)</td><td>{enrichment_completed}</td><td>${enrichment_cost:.4f}</td></tr>
  <tr><td>Generation (Sonnet 5)</td><td>{draft_count}</td><td>${generation_cost:.4f}</td></tr>
  <tr><td><strong>Total pipeline</strong></td><td></td><td><strong>${total:.4f}</strong></td></tr>
  <tr><td>Cost per approved asset</td><td>{approved_count} approved</td><td>${per_approved:.4f}</td></tr>
</tbody>
</table>
"""


def _accounts_table(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT account_id, account_name, category, sites, annual_visits, "
        "is_touring_business, candidate_value_props FROM accounts ORDER BY account_id"
    ).fetchall()
    body = []
    for r in rows:
        props = ", ".join(json.loads(r["candidate_value_props"] or "[]"))
        body.append(
            f"<tr><td>{r['account_id']}</td><td>{_esc(r['account_name'])}</td>"
            f"<td>{_esc(r['category'])}</td><td>{r['sites']}</td>"
            f"<td>{r['annual_visits']:,}</td>"
            f"<td>{'yes' if r['is_touring_business'] else 'no'}</td>"
            f"<td>{_esc(props)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>#</th><th>Account</th><th>Category</th><th>Sites</th>"
        "<th>Annual Visits</th><th>Touring</th><th>Candidate Value Props</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _drafts_table(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT d.*, a.account_name, c.contact_name
        FROM drafts d
        JOIN accounts a ON a.account_id = d.account_id
        LEFT JOIN contacts c ON c.contact_id = d.contact_id
        ORDER BY d.draft_id
        """
    ).fetchall()
    body = []
    for r in rows:
        contact_label = r["contact_name"] or "(briefing)"
        body.append(
            f"<tr><td>{r['draft_id']}</td><td>{_esc(r['draft_type'])}</td>"
            f"<td>{_esc(r['account_name'])}</td><td>{_esc(contact_label)}</td>"
            f"<td class=\"status-{r['status']}\">{_esc(r['status'])}</td>"
            f"<td>${r['estimated_cost_usd']:.4f}</td>"
            f"<td>{r['enrichment_run_id'] if r['enrichment_run_id'] is not None else '—'}</td></tr>"
        )
    return (
        "<table><thead><tr><th>ID</th><th>Type</th><th>Account</th><th>Contact</th>"
        "<th>Status</th><th>Cost</th><th>Enrichment Run</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _enrichment_runs_table(conn: sqlite3.Connection) -> tuple[str, int, int, float]:
    rows = conn.execute(
        """
        SELECT r.*, a.account_name,
               (SELECT COUNT(*) FROM research_evidence e
                WHERE e.enrichment_run_id = r.enrichment_run_id) AS claim_count
        FROM enrichment_runs r
        JOIN accounts a ON a.account_id = r.account_id
        ORDER BY r.enrichment_run_id DESC
        """
    ).fetchall()
    body = []
    completed_count = 0
    completed_cost = 0.0
    for r in rows:
        if r["status"] == "completed":
            completed_count += 1
            completed_cost += r["estimated_cost_usd"] or 0
        status_class = (
            "completed" if r["status"] == "completed"
            else ("failed" if r["status"] == "failed" else "pending_review")
        )
        body.append(
            f"<tr><td>{r['enrichment_run_id']}</td><td>{_esc(r['account_name'])}</td>"
            f"<td>{_badge(r['status'], label=r['status'])}</td>"
            f"<td>{r['claim_count']}</td>"
            f"<td>${(r['estimated_cost_usd'] or 0):.4f}</td>"
            f"<td>{_esc(r['completed_at'])}</td></tr>"
        )
    html_table = (
        "<table><thead><tr><th>Run</th><th>Account</th><th>Status</th><th>Claims</th>"
        "<th>Cost</th><th>Completed At</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )
    return html_table, len(rows), completed_count, completed_cost


def _suppressions_table(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT s.*, c.contact_name, a.account_name
        FROM suppressions s
        LEFT JOIN contacts c ON c.contact_id = s.contact_id
        LEFT JOIN accounts a ON a.account_id = s.account_id
        ORDER BY s.suppression_id
        """
    ).fetchall()
    if not rows:
        return "<p class=\"note\">No suppressions recorded.</p>"
    body = []
    for r in rows:
        body.append(
            f"<tr><td>{_esc(r['account_name'])}</td><td>{_esc(r['contact_name'])}</td>"
            f"<td>{_esc(r['reason'])}</td><td>{_esc(r['source'])}</td>"
            f"<td>{_esc(r['created_at'])}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Account</th><th>Contact</th><th>Reason</th>"
        "<th>Source</th><th>At</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _demo_kpis(conn: sqlite3.Connection) -> dict:
    pending = 0
    approved = 0

    for account_name, contact_name in DEMO_MESSAGES:
        try:
            account = get_account_by_name(conn, account_name)
            contact = get_contact_by_name(conn, account["account_id"], contact_name)
            draft = _latest_campaign_draft(conn, account["account_id"], contact["contact_id"])
        except ValueError:
            pending += 1
            continue
        if not draft:
            pending += 1
            continue
        if draft["status"] == "pending_review":
            pending += 1
        elif draft["status"] in ("approved", "approved_with_edits"):
            approved += 1
        elif draft["status"] == "rejected":
            pass

    for account_name in DEMO_BRIEFINGS:
        try:
            account = get_account_by_name(conn, account_name)
            draft = _latest_briefing_draft(conn, account["account_id"])
        except ValueError:
            pending += 1
            continue
        if not draft:
            pending += 1
            continue
        if draft["status"] == "pending_review":
            pending += 1
        elif draft["status"] in ("approved", "approved_with_edits"):
            approved += 1

    return {
        "pending": pending,
        "approved": approved,
    }


def render_dashboard(db_path: str = str(DB_PATH), out_path: str = str(OUTPUT_PATH)) -> None:
    conn = get_connection(db_path)
    ensure_schema(conn)
    ensure_queue_schema(conn)

    account_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    generation_cost = conn.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM drafts"
    ).fetchone()[0]
    draft_count = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]

    enrichment_html, _, enrichment_completed, enrichment_cost = _enrichment_runs_table(conn)
    kpis = _demo_kpis(conn)
    demo_claims_html, demo_claim_count = _demo_research_dossiers(conn)
    total_cost = enrichment_cost + generation_cost

    pipeline_html = _demo_pipeline_table(conn)
    content_html = _demo_content_cards(conn)
    events_html, event_count = _events_table(conn)
    economics_html = _economics_table(
        enrichment_cost, enrichment_completed, generation_cost, draft_count, kpis["approved"]
    )

    accounts_html = _accounts_table(conn)
    drafts_html = _drafts_table(conn)
    suppressions_html = _suppressions_table(conn)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Easol AI Outbound — Pilot Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<h1>Easol AI Outbound — Pilot Dashboard</h1>
<p class="subtitle">Research &rarr; Generate &rarr; Review &rarr; Send</p>
<p class="meta">
  Static export generated {generated_at} · Read-only presentation view.<br>
  To approve or reject drafts, run <code>python src/review.py</code> then re-run this script.
</p>

<nav class="nav">
  <a href="#pipeline">Pipeline</a>
  <a href="#content">Generated content</a>
  <a href="#research">Research dossiers</a>
  <a href="#audit">Review audit</a>
  <a href="#economics">Economics</a>
  <a href="#appendix">Appendix</a>
</nav>

<div class="cards">
  <div class="card"><div class="value">{DEMO_ASSET_COUNT}</div><div class="label">Demo assets</div></div>
  <div class="card"><div class="value">{kpis['pending']}</div><div class="label">Pending review</div></div>
  <div class="card"><div class="value">{kpis['approved']}</div><div class="label">Approved</div></div>
  <div class="card"><div class="value">${total_cost:.4f}</div><div class="label">Total pipeline cost</div></div>
  <div class="card"><div class="value">{demo_claim_count}</div><div class="label">Cited claims (demo)</div></div>
</div>

<h2 id="pipeline">Demo pipeline</h2>
<p class="note">Pilot scope: 5 four-touch campaigns + 2 call briefings across {len(demo_account_names())} accounts.</p>
{pipeline_html}

<h2 id="content">Generated content</h2>
{content_html}

<h2 id="research">Research dossiers</h2>
<p class="note">Latest completed enrichment per demo account — shared by campaigns and briefings.</p>
{demo_claims_html}

<h2 id="audit">Review audit trail</h2>
<p class="note">{event_count} event(s) · rejected rows highlighted · run review.py to add more.</p>
{events_html}

<h2 id="economics">Economics</h2>
{economics_html}

<h2 id="appendix">Appendix</h2>
<details>
  <summary>All drafts ({draft_count})</summary>
  {drafts_html}
</details>
<details>
  <summary>All enrichment runs</summary>
  {enrichment_html}
</details>
<details>
  <summary>Suppressions</summary>
  {suppressions_html}
</details>
<details>
  <summary>All accounts scored ({account_count})</summary>
  {accounts_html}
</details>

</body>
</html>
"""

    conn.close()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(page, encoding="utf-8")
    print(f"Dashboard written to {out_path}")


def main() -> None:
    render_dashboard()


if __name__ == "__main__":
    main()
