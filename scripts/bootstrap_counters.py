"""One-time ops script: seed the `counters` collection from a SQLite dump.

MongoDB has no AUTOINCREMENT; this codebase mirrors it with a `counters`
collection. A fresh dump has data whose ids already start at 1, so before the
first write the counters MUST be advanced to the existing maxima — otherwise
the next forecast run would be created with run_id=1 and collide with the
migrated run 1 (values for two different runs would be returned together).

Also drops the inert `sqlite_sequence` collection that some dump tools carry
over (it is SQLite's internal AUTOINCREMENT bookkeeping and has no meaning in
MongoDB).

Idempotent and safe: a counter is only ever moved UP (max(current, existing)).
Run it as part of bring-up; init_mongodb_indexes.py calls the same routine.

  .venv\\Scripts\\python.exe scripts\\bootstrap_counters.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src" / "data_layer"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT / "config" / ".env")
import db_client  # noqa: E402


def bootstrap_counters(db) -> dict:
    """Align sequence counters with maxima already present in the database,
    and shim SQLite column names onto the field names this codebase writes."""
    # Schema shim: the SQLite dump stores the message ordinal as `id`; this
    # codebase writes/reads `seq`. Copy it once for migrated docs so history
    # ordering and the API `id` field behave exactly as before (idempotent).
    shimmed = db.chat_messages.update_many(
        {"seq": {"$exists": False}, "id": {"$exists": True}},
        [{"$set": {"seq": "$id"}}],
    )

    plans = {
        "forecast_run_id": (db.forecast_runs, "run_id"),
        "ingestion_log_id": (db.ingestion_log, "id"),
        "chat_message_seq": (db.chat_messages, "seq"),
    }
    result: dict[str, dict] = {}
    if shimmed.modified_count:
        result["chat_messages.seq<-"+"id"] = {"shimmed_docs": shimmed.modified_count}
    for counter_name, (collection, id_field) in plans.items():
        top = collection.find_one(sort=[(id_field, -1)]) or {}
        existing_max = int(top.get(id_field) or 0)
        current = db.counters.find_one({"_id": counter_name})
        current_seq = int(current["seq"]) if current else 0
        if existing_max > current_seq:
            db.counters.update_one({"_id": counter_name},
                                   {"$set": {"seq": existing_max}}, upsert=True)
            result[counter_name] = {"was": current_seq, "now": existing_max,
                                    "source": f"max({collection.name}.{id_field})"}
        else:
            result[counter_name] = {"was": current_seq, "now": current_seq,
                                    "source": "unchanged"}
    if "sqlite_sequence" in db.list_collection_names():
        db.sqlite_sequence.drop()
        result["sqlite_sequence"] = "dropped (inert SQLite bookkeeping)"
    return result


def main() -> int:
    try:
        db = db_client.get_db()
        print(json.dumps({"database": db.name, "counters": bootstrap_counters(db),
                          "live_counters": [(d["_id"], d["seq"])
                                            for d in db.counters.find({}).sort("_id", 1)]},
                         indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"COUNTER BOOTSTRAP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
