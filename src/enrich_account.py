"""Stage 3 — enrichment CLI (PRD §5, §11). Runs one company dossier per account
via web search, independent of message/briefing generation. Every invocation
always performs a new search and appends a new enrichment_runs row (history is
never overwritten). This is the only place research_evidence gets written —
generators only read the latest completed enrichment via
research.get_latest_enrichment().

Run: python src/enrich_account.py --account "Puttshack"
     python src/enrich_account.py --all-demo
"""

import argparse
import sqlite3

from context_builder import demo_account_names, get_account_by_name
from db import DB_PATH, get_connection, report_failures, run_batch
from research import create_enrichment_run


def enrich_one(conn: sqlite3.Connection, account_name: str) -> None:
    account = get_account_by_name(conn, account_name)
    run_id = create_enrichment_run(conn, account["account_id"])
    claim_count = conn.execute(
        "SELECT COUNT(*) FROM research_evidence WHERE enrichment_run_id = ?", (run_id,)
    ).fetchone()[0]
    cost = conn.execute(
        "SELECT estimated_cost_usd FROM enrichment_runs WHERE enrichment_run_id = ?", (run_id,)
    ).fetchone()[0]
    print(
        f"{account_name}: enrichment_run_id={run_id} "
        f"{claim_count} claim(s), cost=${(cost or 0):.4f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich one or more accounts with a company research dossier (always web search)"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--account", help='Account name to enrich, exact match (e.g. "Puttshack")'
    )
    target.add_argument(
        "--all-demo", action="store_true",
        help="Enrich every account used by the demo message/briefing samples",
    )
    args = parser.parse_args()

    account_names = demo_account_names() if args.all_demo else [args.account]

    conn = get_connection(DB_PATH)
    _, failures = run_batch(account_names, lambda name: enrich_one(conn, name))
    conn.close()
    report_failures(failures)


if __name__ == "__main__":
    main()
