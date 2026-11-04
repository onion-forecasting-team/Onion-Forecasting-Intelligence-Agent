"""DR restore: rebuild the MongoDB store from the retained SQLite backup.

This is the canonical offline restore path after migration. It REPLACES the
mapped collections with rows read from data/onion_forecasting_v4.sqlite3 so
the store ends up in exactly the shape the application expects:

- observations / daily_price_observations -> _id = timestamp
- ap_monthly_observations                 -> _id = month_start
- forecast_runs                           -> _id = str(run_id)
- forecast_values / ingestion_log         -> same columns (+ version: 1)
- chat_messages                           -> id kept, seq = id (code shim)
- source/graph sync-state collections     -> same columns (first row only)

It then re-seeds sequence counters. Idempotent and DANGEROUS by design: it
drops the mapped collections first, so it REQUIRES --yes.

  .venv\\Scripts\\python.exe scripts\\restore_from_sqlite_backup.py --yes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src" / "data_layer"))
sys.path.insert(0, str(PROJECT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT / "config" / ".env")
import db_client  # noqa: E402
import history_store  # noqa: E402
from bootstrap_counters import bootstrap_counters  # noqa: E402

V = lambda: history_store.SCHEMA_VERSION  # noqa: E731


def rows_to_docs(rows, key_fields: tuple[str, ...], natural: str | None,
                 mutate=None) -> list[dict]:
    docs = []
    seen: set = set()
    for row in rows:
        record = {key: row[key] for key in row.keys()}
        if mutate:
            record = mutate(record)
        if natural is None:
            docs.append(record)  # keep for caller-side dedupe using key_fields
            continue
        key = {}
        for field in key_fields:
            key[field] = record[field]
        dedupe_key = json.dumps(key, sort_keys=True, default=str)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        docs.append({"_id": str(record[natural]), **record})
    return docs


def restore_collection(db, collection: str, docs: list[dict]) -> dict:
    db[collection].drop()
    db_client.reset_index_flag(db.name)
    if docs:
        db[collection].insert_many(docs, ordered=False)
    return {"docs": len(docs)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="required confirmation: collections are dropped")
    args = parser.parse_args()
    if not args.yes:
        print("REFUSING: this restore drops collections; re-run with --yes.",
              file=sys.stderr)
        return 2
    if not history_store.DB_PATH.exists():
        print(f"SQLite backup not found at {history_store.DB_PATH}.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(history_store.DB_PATH)
    conn.row_factory = sqlite3.Row
    db = db_client.get_db()

    def _chat_msg(record: dict) -> dict:
        record["seq"] = record["id"]
        record["version"] = V()
        return record

    def _version(record: dict) -> dict:
        record["version"] = V()
        return record

    report: dict[str, dict] = {}

    observations = conn.execute("SELECT * FROM observations ORDER BY timestamp").fetchall()
    report["observations"] = restore_collection(
        db, "observations", rows_to_docs(observations, ("timestamp",), "timestamp", _version))

    daily = conn.execute("SELECT * FROM daily_price_observations ORDER BY timestamp").fetchall()
    report["daily_price_observations"] = restore_collection(
        db, "daily_price_observations",
        rows_to_docs(daily, ("timestamp",), "timestamp", _version))

    monthly = conn.execute("SELECT * FROM ap_monthly_observations ORDER BY month_start").fetchall()
    report["ap_monthly_observations"] = restore_collection(
        db, "ap_monthly_observations",
        rows_to_docs(monthly, ("month_start",), "month_start", _version))

    runs = conn.execute("SELECT * FROM forecast_runs").fetchall()
    report["forecast_runs"] = restore_collection(
        db, "forecast_runs", rows_to_docs(runs, ("run_id",), "run_id", _version))

    values = conn.execute("SELECT * FROM forecast_values").fetchall()
    report["forecast_values"] = restore_collection(
        db, "forecast_values", rows_to_docs(values, ("run_id", "state_code",
                                                    "target_key", "forecast_date"), None, _version))

    logs = conn.execute("SELECT * FROM ingestion_log").fetchall()
    report["ingestion_log"] = restore_collection(
        db, "ingestion_log", rows_to_docs(logs, ("id",), None, _version))

    sync_state = conn.execute("SELECT * FROM source_sync_state").fetchall()
    report["source_sync_state"] = restore_collection(
        db, "source_sync_state", rows_to_docs(sync_state, ("source_path",), None))

    graph_state = conn.execute("SELECT * FROM graph_dataset_sync_state").fetchall()
    report["graph_dataset_sync_state"] = restore_collection(
        db, "graph_dataset_sync_state", rows_to_docs(graph_state, ("dataset_key",), None))

    sessions = conn.execute("SELECT * FROM chat_sessions").fetchall()
    report["chat_sessions"] = restore_collection(
        db, "chat_sessions", rows_to_docs(sessions, ("session_id",), None, _version))

    messages = conn.execute("SELECT * FROM chat_messages").fetchall()
    report["chat_messages"] = restore_collection(
        db, "chat_messages", rows_to_docs(messages, ("id",), None, _chat_msg))
    conn.close()

    db_client.ensure_indexes(db)
    counters = bootstrap_counters(db)

    print(json.dumps({"database": db.name, "restored": report,
                      "counters": counters},
                     indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
