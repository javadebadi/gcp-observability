"""
Track players who used promo keys instead of paying.

Use case
--------
Game logs contain lines like:

    player 2342342 increased level of heart to 234 with promo key instead of paying

This script:
  1. Syncs matching INFO logs from Cloud Logging into a local SQLite store,
     in 30-minute windows starting from 2026-01-04.
  2. On each run it resumes from where it left off — no windows are re-fetched.
     If you haven't run it in a week, it catches up all missing windows on the
     next run automatically.
  3. Reads from the local store (no Cloud Logging charges) and prints each
     player's promo usage.

Usage
-----
    # First run — fetches from 2026-01-04 to now in 30-min windows
    python examples/promo_key_tracker.py

    # Subsequent runs — picks up from the last watermark, catches up to now
    python examples/promo_key_tracker.py

Configuration
-------------
Set PROJECT to your GCP project ID and DB_PATH to where you want the
SQLite database file to live.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

from gcp_observability import Client, QueryBuilder, SQLiteStore, Syncer, Severity

# ── Configuration ──────────────────────────────────────────────────────────────

PROJECT    = "my-gcp-project"       # your GCP project ID
DB_PATH    = "promo_logs.db"        # local SQLite file (created on first run)
SYNC_ID    = "promo-key-usage"      # unique name for this sync job
START_DATE = datetime(2026, 1, 4, tzinfo=timezone.utc)

# ── Log pattern ────────────────────────────────────────────────────────────────

# Matches: "player 2342342 increased level of heart to 234 with promo key ..."
_PATTERN = re.compile(
    r"player\s+(\d+)\s+increased level of heart to\s+(\d+)\s+with promo key"
)

# ── Sync ───────────────────────────────────────────────────────────────────────

def sync() -> SQLiteStore:
    """
    Pull matching logs from Cloud Logging into local storage.

    Resumes from the last watermark on every run — windows already fetched
    are never re-queried. If 49 intervals were missed, all 49 are fetched on
    the next run.
    """
    client = Client()
    store  = SQLiteStore(DB_PATH)
    syncer = Syncer(client, store)

    watermark = store.get_watermark(SYNC_ID)
    if watermark:
        print(f"Resuming from watermark: {watermark.isoformat()}")
    else:
        print(f"First run — starting from {START_DATE.date()}")

    results = syncer.backfill(
        QueryBuilder()
            .severity_eq(Severity.INFO)
            .global_search("promo key instead of paying"),
        project=PROJECT,
        sync_id=SYNC_ID,
        start=START_DATE,
        # end defaults to now — capped automatically, never goes into the future
        window_hours=0.5,   # 30-minute intervals
    )

    windows_with_data = [r for r in results if r.fetched > 0]
    total_fetched = sum(r.fetched for r in results)
    total_stored  = sum(r.stored  for r in results)

    print(f"Processed {len(results)} windows  "
          f"({len(windows_with_data)} had data)  "
          f"fetched={total_fetched}  stored={total_stored}")
    print(f"New watermark: {store.get_watermark(SYNC_ID).isoformat()}")
    return store


# ── Analyzer ───────────────────────────────────────────────────────────────────

def analyze(store: SQLiteStore) -> None:
    """
    Read promo-key log entries from the local store and print a summary
    of player_id and the total promo amount each player received.
    """
    entries = store.query(
        search="promo key instead of paying",
        limit=100_000,
        order="asc",
    )

    if not entries:
        print("\nNo promo key entries found in local store.")
        return

    # player_id → list of promo amounts
    player_promos: dict[str, list[int]] = defaultdict(list)
    unmatched = 0

    for entry in entries:
        # payload is either a plain string or a dict with a "message" key
        text = (
            entry.payload
            if isinstance(entry.payload, str)
            else entry.payload.get("message", "")
        )
        match = _PATTERN.search(text)
        if match:
            player_id  = match.group(1)
            promo_amount = int(match.group(2))
            player_promos[player_id].append(promo_amount)
        else:
            unmatched += 1

    print(f"\nAnalyzed {len(entries)} entries  "
          f"({len(player_promos)} unique players  {unmatched} unmatched)\n")

    # Sort by total promo amount descending
    ranked = sorted(
        player_promos.items(),
        key=lambda item: sum(item[1]),
        reverse=True,
    )

    header = f"{'player_id':<15}  {'uses':>6}  {'total_amount':>14}  {'avg_amount':>12}"
    print(header)
    print("-" * len(header))
    for player_id, amounts in ranked:
        print(
            f"{player_id:<15}  "
            f"{len(amounts):>6}  "
            f"{sum(amounts):>14}  "
            f"{sum(amounts) / len(amounts):>11.1f}"
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    store = sync()
    analyze(store)
