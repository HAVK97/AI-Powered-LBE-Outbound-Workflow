"""Stage 1 — ingest (PRD §5). CSV -> accounts/contacts tables.

Run: python src/load_data.py   (from repo root; see PRD §18 import strategy)

Safe by default: refuses to reload if research/drafts/events already exist.
Pass --reset to wipe dependent tables (FK order) and reload from CSV.
"""

import argparse
import csv
import sqlite3

from context_builder import is_touring_business
from db import DB_PATH, REPO_ROOT, get_connection
from research import ensure_schema

DEFAULT_CSV_PATH = REPO_ROOT / "data" / "lbe_target_accounts.csv"
DEFAULT_DB_PATH = DB_PATH
SCHEMA_PATH = REPO_ROOT / "src" / "schema.sql"

# FK-safe wipe order: children before parents. Tables added after the
# original wipe list (draft_touches, apollo_push_runs, contact_enrichment_runs)
# must appear before the parents they reference.
DEPENDENT_TABLES = (
    "draft_events",
    "draft_touches",
    "apollo_push_runs",
    "suppressions",
    "drafts",
    "research_evidence",
    "enrichment_runs",
    "contact_enrichment_runs",
    "contacts",
    "accounts",
)


def _to_int(value: str) -> int:
    return int(value.replace(",", "").strip())


def _to_float(value: str) -> float:
    return float(value.replace(",", "").strip())


def _has_dependent_state(conn: sqlite3.Connection) -> bool:
    for table in (
        "enrichment_runs",
        "research_evidence",
        "drafts",
        "draft_events",
        "draft_touches",
        "apollo_push_runs",
        "contact_enrichment_runs",
        "suppressions",
    ):
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if count:
            return True
    return False


def _reset_all(conn: sqlite3.Connection) -> None:
    # Break contacts <-> contact_enrichment_runs cycle before wiping either.
    try:
        conn.execute(
            "UPDATE contacts SET resolved_email = NULL, resolved_email_run_id = NULL"
        )
    except sqlite3.OperationalError:
        pass
    for table in DEPENDENT_TABLES:
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass


def load_csv(csv_path: str, db_path: str, *, reset: bool = False) -> None:
    conn = get_connection(db_path)
    # Migrate legacy DBs first: schema.sql CREATE TABLE IF NOT EXISTS won't
    # ALTER existing tables, but its CREATE INDEX ... (enrichment_run_id) will
    # fail if those columns are missing. ensure_schema adds them idempotently.
    ensure_schema(conn)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())

    if _has_dependent_state(conn) and not reset:
        conn.close()
        raise SystemExit(
            "Refusing to reload: enrichment/research/drafts/touches/events/"
            "apollo_push/contact_enrichment/suppressions already exist. "
            "Re-run with --reset to wipe them, or leave the DB alone and "
            "continue with enrich_account.py / generate_*.py."
        )

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    _reset_all(conn)

    seen_accounts: set[int] = set()
    accounts_inserted = 0
    contacts_inserted = 0

    for row in rows:
        account_id = _to_int(row["#"])
        sites = _to_int(row["Sites"])

        if account_id not in seen_accounts:
            seen_accounts.add(account_id)
            annual_visits = _to_int(row["Est. Annual Visits"])
            avg_ticket_price = _to_float(row["Est. Avg Ticket Price ($)"])
            total_transaction_volume = _to_float(row["Est. Total Transaction Volume ($)"])
            easol_annual_revenue = _to_float(row["Est. Easol Annual Revenue ($)"])
            conn.execute(
                """
                INSERT OR REPLACE INTO accounts (
                    account_id, account_name, category, sub_category, description,
                    hq_location, region, sites, is_touring_business,
                    annual_visits, avg_ticket_price, total_transaction_volume,
                    easol_annual_revenue, candidate_value_props
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    account_id,
                    row["Account Name"],
                    row["Category"],
                    row["Sub-Category"],
                    row["Description"],
                    row["HQ Location"],
                    row["Region"],
                    sites,
                    is_touring_business(sites),
                    annual_visits,
                    avg_ticket_price,
                    total_transaction_volume,
                    easol_annual_revenue,
                ),
            )
            accounts_inserted += 1

        conn.execute(
            """
            INSERT INTO contacts (
                account_id, contact_name, contact_title, contact_category,
                role_default_value_props
            ) VALUES (?, ?, ?, ?, NULL)
            """,
            (
                account_id,
                row["Contact Name"],
                row["Contact Title"],
                row["Contact Category"],
            ),
        )
        contacts_inserted += 1

    conn.commit()
    conn.close()
    print(f"Loaded {accounts_inserted} accounts, {contacts_inserted} contacts into {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load LBE target accounts CSV into SQLite")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe enrichment/research/drafts/events/suppressions before reloading (destructive)",
    )
    args = parser.parse_args()

    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    load_csv(str(DEFAULT_CSV_PATH), str(DEFAULT_DB_PATH), reset=args.reset)


if __name__ == "__main__":
    main()
