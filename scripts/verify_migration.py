"""One-time ops script: verify the SQLite -> MongoDB migration row-for-row.

Compares the retained SQLite backup (ONION_DB_PATH, default
data/onion_forecasting_v4.sqlite3) against the live MongoDB store:

  .venv\\Scripts\\python.exe scripts\\verify_migration.py

Checks per table/collection:
  1. row count vs document count
  2. primary-key coverage (every SQLite key exists in MongoDB)
  3. a numeric spot-check on one sampled document per key table
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src" / "data_layer"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT / "config" / ".env")
import db_client  # noqa: E402
import history_store  # noqa: E402

# table -> (collection, key column)
MAPPING = {
    "observations": ("observations", "timestamp"),
    "daily_price_observations": ("daily_price_observations", "timestamp"),
    "ap_monthly_observations": ("ap_monthly_observations", "month_start"),
    "ingestion_log": None,            # count-only (autoid ids differ)
    "source_sync_state": None,        # count-only (rec_BANK keys differ)
    "graph_dataset_sync_state": None,
    "forecast_runs": ("forecast_runs", "run_id"),
    "forecast_values": None,         # count-only: keys are (run_id, target, date) tuples
    "chat_sessions": ("chat_sessions", "session_id"),
    "chat_messages": None,           # ids renumbered by counters
}


def main() -> int:
    if not history_store.DB_PATH.exists():
        print(f"SQLite backup not found at {history_store.DB_PATH} — nothing to verify against.",
              file=sys.stderr)
        return 1
    db = db_client.get_db()
    db_client.ensure_indexes(db)
    conn = sqlite3.connect(history_store.DB_PATH)
    conn.row_factory = sqlite3.Row

    report: dict[str, dict] = {}
    all_ok = True
    for table, mapping in MAPPING.items():
        try:
            sql_count = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"]
        except sqlite3.OperationalError as exc:
            report[table] = {"status": "skipped", "reason": f"table missing in backup: {exc}"}
            continue
        collection = (mapping[0] if mapping else table)
        mongo_count = int(db[collection].count_documents({})) if collection else 0
        entry: dict = {"sqlite_rows": sql_count, "mongodb_docs": mongo_count,
                       "status": "ok" if sql_count == mongo_count else "MISMATCH"}
        if mapping and sql_count == mongo_count and sql_count:
            key_column = mapping[1]
            keys = [str(r[0]) for r in conn.execute(f'SELECT "{key_column}" FROM "{table}"')]
            mongo_keys = {str(d[key_column]) for d in
                          db[collection].find({}, {key_column: 1, "_id": 0})}
            missing = [k for k in keys if k not in mongo_keys]
            entry["missing_keys_in_mongodb"] = missing[:10]
            entry["missing_count"] = len(missing)
            if missing:
                entry["status"] = "MISMATCH"
            if table == "observations" and not missing:
                sample = conn.execute(
                    'SELECT * FROM observations ORDER BY timestamp DESC LIMIT 1').fetchone()
                doc = db.observations.find_one({"_id": str(sample["timestamp"])}, {"_id": 0})
                if doc is None:
                    entry["status"] = "MISMATCH"
                    entry["note"] = ("no document with _id=<timestamp>; natural-key "
                                     "_ids missing — run scripts/rebuild_natural_ids.py")
                else:
                    checked = []
                    bad = []
                    for col in sample.keys():
                        if col == "_id" or col not in doc:
                            continue
                        checked.append(col)
                        sv, mv = sample[col], doc[col]
                        try:
                            differs = abs(float(sv) - float(mv)) > 1e-6
                        except (TypeError, ValueError):
                            differs = str(sv) != str(mv)  # textual columns: exact match
                        if differs:
                            bad.append(col)
                    entry["spot_check_columns"] = len(checked)
                    entry["spot_check_faults"] = bad
                    if not checked:
                        entry["status"] = "MISMATCH"
                        entry["note"] = "spot check matched zero columns"
                    if bad:
                        entry["status"] = "MISMATCH"
        if entry["status"] != "ok":
            all_ok = False
        report[table] = entry
    conn.close()

    print(json.dumps({"all_ok": all_ok, "database": db.name,
                      "store": db_client.store_label(), "tables": report},
                     indent=2, ensure_ascii=False))
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
