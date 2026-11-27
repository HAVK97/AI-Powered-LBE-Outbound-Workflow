"""Shared DB plumbing (paths, connection factory, migration helper, batch runner).

Every script in this repo used to hardcode its own REPO_ROOT/DB_PATH, its own
sqlite3.connect() + PRAGMA foreign_keys + row_factory boilerplate, its own
copy of "_add_column_if_missing", and its own per-item try/except loop in
main(). All of that lived here once instead of being copy-pasted N times.
"""

import sys
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "db" / "outbound.db"


def get_connection(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """Idempotent ALTER TABLE, used by every module's ensure_schema() to
    upgrade a pre-existing db/outbound.db created before a given column existed.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


T = TypeVar("T")
R = TypeVar("R")


def run_batch(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    item_name: Callable[[T], str] = str,
) -> tuple[list[R], list[str]]:
    """Run fn(item) for every item; never let one failure stop the batch.

    Returns (results, failures) — callers keep their own success-summary
    print (wording differs per script) and should call report_failures()
    with the second element once done.
    """
    results: list[R] = []
    failures: list[str] = []
    for item in items:
        name = item_name(item)
        try:
            results.append(fn(item))
        except Exception as e:
            failures.append(f"{name}: {e}")
            print(f"FAILED {name}: {e}", flush=True)
    return results, failures


def report_failures(failures: list[str]) -> None:
    """Print any collected failures and exit(1) — matches the exit behaviour
    every CLI script in this repo already had, just no longer hand-rolled.
    """
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
