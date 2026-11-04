"""One-time ops script: import forecast runs missed by the sqlite->mongo dump.

The dump tool missed rows written after the snapshot was taken (observed:
SQLite backup had run_id 38 with 20 values; MongoDB had none of them).
This script imports ONLY the run_ids absent from MongoDB, writing documents
in the exact shape history_store.save_forecast_run produces, then re-seeds
the sequence counters (so run_id numbering continues correctly).

Idempotent: existing run_ids are skipped; re-runs import nothing.

  .venv\\Scripts\\python.exe scripts\\import_missing_forecasts.py
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
import history_store  # noqa: E402  (for DB_PATH + SCHEMA_VERSION)
from bootstrap_counters import bootstrap_counters  # noqa: E402


def main() -> int:
    if not history_store.DB_PATH.exists():
        print(f"SQLite backup not found at {history_store.DB_PATH}; nothing to import.",
              file=sys.stderr)
        return 1
    db = db_client.get_db()
    conn = sqlite3.connect(history_store.DB_PATH)
    conn.row_factory = sqlite3.Row

    mongo_run_ids = {int(r["run_id"]) for r in
                     db.forecast_runs.find({}, {"run_id": 1, "_id": 0})}
    sqlite_runs = conn.execute("SELECT * FROM forecast_runs").fetchall()
    missing = [row for row in sqlite_runs if int(row["run_id"]) not in mongo_run_ids]

    imported: list[int] = []
    for row in missing:
        run_id = int(row["run_id"])
        run_doc = {k: row[k] for k in row.keys() if k != "run_id"}
        run_doc["run_id"] = run_id
        run_doc.pop("_id", None)
        run_doc["version"] = history_store.SCHEMA_VERSION
        db.forecast_runs.insert_one({"_id": str(run_id), **run_doc})
        values = conn.execute(
            "SELECT state_code, target_key, forecast_date, p10, p50, p90, unit "
            "FROM forecast_values WHERE run_id=?", (run_id,)).fetchall()
        if values:
            db.forecast_values.insert_many([
                {"run_id": run_id, "state_code": v["state_code"],
                 "target_key": v["target_key"], "forecast_date": v["forecast_date"],
                 "p10": float(v["p10"]), "p50": float(v["p50"]), "p90": float(v["p90"]),
                 "unit": v["unit"], "version": history_store.SCHEMA_VERSION}
                for v in values
            ])
        imported.append({"run_id": run_id, "values": len(values)})
    conn.close()

    counter_report = bootstrap_counters(db)
    print(json.dumps({"database": db.name,
                      "runs_in_sqlite": len(sqlite_runs),
                      "runs_in_mongodb_before": len(mongo_run_ids),
                      "imported": imported,
                      "counters": counter_report},
                     indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
