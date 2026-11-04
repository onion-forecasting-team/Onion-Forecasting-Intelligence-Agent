"""One-time ops script: create MongoDB indexes, stamp schema versions, verify.

Run from the project root after rotating the Atlas password and whitelisting
this machine's IP:

  .venv\\Scripts\\python.exe scripts\\init_mongodb_indexes.py

Idempotent: safe to run repeatedly.
1. Connectivity check (ping).
2. Creates every planned index (compound included).
3. Backfills `version: 1` onto pre-migration documents missing it.
4. Warns if collections still use plan-alias names (see align_collection_names.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src" / "data_layer"))
sys.path.insert(0, str(PROJECT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT / "config" / ".env")
import db_client  # noqa: E402
from bootstrap_counters import bootstrap_counters  # noqa: E402

ALIAS_CHECK = {
    "daily_prices": "daily_price_observations",
    "monthly_balances": "ap_monthly_observations",
    "ingestion_logs": "ingestion_log",
}


def main() -> int:
    try:
        db_client._require_uri()
        client = db_client.get_client()
        db = client[db_client.database_name()]
        hello = client.admin.command("hello")

        # 1) Indexes.
        db_client.reset_index_flag(db.name)
        db_client.ensure_indexes(db)

        # 2) Sequence counters aligned with existing maxima (SQLite AUTOINCREMENT
        #    parity — mandatory before the first post-migration write).
        counter_report = bootstrap_counters(db)

        # 3) Schema-version backfill for pre-migration documents.
        backfilled = {}
        for collection, specs in db_client._INDEX_PLAN.items():
            targeted = db[collection]
            result = targeted.update_many({"version": {"$exists": False}},
                                          {"$set": {"version": db_client.SCHEMA_VERSION}})
            if result.modified_count:
                backfilled[collection] = result.modified_count

        # 3) Alias-name detection.
        existing = set(db.list_collection_names())
        alias_hits = {alias: canonical for alias, canonical in ALIAS_CHECK.items()
                      if alias in existing and canonical not in existing}

        print(json.dumps({
            "status": "indexes ready" if not alias_hits else "indexes ready (alias names detected)",
            "database": db.name,
            "store": db_client.store_label(),
            "replica_set": hello.get("setName"),
            "counters": counter_report,
            "schema_version_backfilled": backfilled,
            "alias_names_detected": alias_hits,
            "next_step": ("run scripts/align_collection_names.py to rename alias "
                          "collections to the canonical schema names"
                          if alias_hits else None),
            "collections": sorted(existing),
        }, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(f"INDEX INIT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        hint = ("Fix connectivity first: 1) Atlas -> Network Access -> add your current "
                "IP or 0.0.0.0/0; 2) verify MONGO_URI in config/.env (rotated "
                "credentials only); 3) ensure the cluster is not paused.")
        print(f"HINT: {hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
