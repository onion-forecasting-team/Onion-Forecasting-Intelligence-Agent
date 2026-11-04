"""One-time ops script: rebuild natural-key `_id`s on a migrated SQLite dump.

The SQLite dump was imported with default ObjectId `_id`s, but this codebase
upserts on natural keys exactly like the former SQL primary keys:
    observations            -> _id = timestamp  ("YYYY-MM-DD")
    daily_price_observations-> _id = timestamp  ("YYYY-MM-DD")
    ap_monthly_observations -> _id = month_start("YYYY-MM-DD")
    forecast_runs           -> _id = str(run_id)
Without this, `find_one({"_id": stamp})` misses migrated rows and the next
ingestion inserts a duplicate row for the same date.

Behavior:
- Rebuilds `_id` in-place (insert natural-key copies, then delete the ObjectId
  originals) — collection count stays identical except for exact-duplicate
  natural keys, which are deduplicated (first row kept, dupes dropped).
- Idempotent: re-running performs no changes.
- Collections whose lookups never use `_id` (forecast_values, ingestion_log,
  chat_*) keep their ObjectId `_id`s — matching what the code writes.

  .venv\\Scripts\\python.exe scripts\\rebuild_natural_ids.py
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

# collection -> field holding the natural key
NATURAL_KEYS = {
    "observations": "timestamp",
    "daily_price_observations": "timestamp",
    "ap_monthly_observations": "month_start",
    "forecast_runs": "run_id",
}


def natural_id(collection: str, doc: dict) -> str | None:
    field = NATURAL_KEYS[collection]
    value = doc.get(field)
    if value is None:
        return None
    return str(value)


def rebuild_collection(db, collection: str) -> dict:
    field = NATURAL_KEYS[collection]
    docs = list(db[collection].find({}))
    old_ids = [doc["_id"] for doc in docs if not isinstance(doc["_id"], str)]
    if not old_ids:
        return {"changed": False, "reason": "all _ids are already natural keys",
                "docs": len(docs)}

    by_key: dict[str, dict] = {}
    duplicates = 0
    for doc in docs:
        key = natural_id(collection, doc)
        if key is None:
            raise SystemExit(f"FATAL: {collection} doc {doc['_id']} has no '{field}'.")
        if key in by_key:
            duplicates += 1  # same natural key twice: keep the first, drop this copy
        else:
            by_key[key] = doc

    inserted = 0
    already_present = 0
    for key, doc in by_key.items():
        body = {k: v for k, v in doc.items() if k != "_id"}
        try:
            db[collection].insert_one({"_id": key, **body})
            inserted += 1
        except Exception as exc:
            # DuplicateKeyError == the natural-key copy already exists from an
            # interrupted earlier run; that copy holds the same content.
            if "duplicate key" not in str(exc).lower() and "E11000" not in str(exc):
                raise
            already_present += 1

    db[collection].delete_many({"_id": {"$in": old_ids}})

    remaining_objectid = db[collection].count_documents({"_id": {"$type": "objectId"}})
    if remaining_objectid:
        # Interrupted-run leftovers for keys whose str copy already existed are
        # safe to drop only if every ObjectId doc's key has a natural-key twin.
        leftovers = list(db[collection].find({"_id": {"$type": "objectId"}}))
        loseable = []
        for doc in leftovers:
            key = natural_id(collection, doc)
            if db[collection].count_documents({"_id": key}) > 0:
                loseable.append(doc["_id"])
            else:
                raise SystemExit(
                    f"FATAL: {collection} ObjectId doc {doc['_id']} (key {key}) has no "
                    f"natural-key twin; manual inspection required.")
        db[collection].delete_many({"_id": {"$in": loseable}})

    return {"changed": True, "rebuilt": inserted, "already_present_skipped": already_present,
            "duplicates_dropped": duplicates,
            "docs_after": db[collection].count_documents({})}


def main() -> int:
    db = db_client.get_db()
    report = {}
    for collection in NATURAL_KEYS:
        report[collection] = rebuild_collection(db, collection)
    counts = {collection: db[collection].count_documents({}) for collection in NATURAL_KEYS}
    report["_final_counts"] = counts
    print(json.dumps({"database": db.name, "result": report},
                     indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
