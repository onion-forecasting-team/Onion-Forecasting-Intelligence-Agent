"""One-time repair: flatten the nested Y -> S -> R district document into a top-level ``YSR`` key.

The district datasets carry a ``Y.S.R`` CSV column (YSR Kadapa). MongoDB ingestion
expanded the dotted column name into a nested document ``{"Y": {"S": {"R": value}}}``,
which hid the district from consumers. This script rewrites both collections so every
document carries ``YSR`` at the top level and no longer contains the ``Y`` helper.

Idempotent: documents without a nested ``Y`` structure are left untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "data_layer"))

from db_client import get_db  # noqa: E402

COLLECTIONS = ("district_wise_data", "forecast_district_wise_data")


def flatten(doc: dict) -> dict | None:
    """Return the repaired document, or None when nothing needs changing."""
    nested = doc.get("Y")
    if not isinstance(nested, dict):
        return None
    value = nested.get("S", {}).get("R") if isinstance(nested.get("S"), dict) else None
    repaired = {key: val for key, val in doc.items() if key != "Y"}
    repaired["YSR"] = value
    return repaired


def main() -> None:
    db = get_db()
    for name in COLLECTIONS:
        collection = db[name]
        repaired = 0
        skipped = 0
        dropped = 0
        for doc in collection.find({}, {}):
            fixed = flatten(doc)
            if fixed is None:
                skipped += 1
                continue
            result = collection.replace_one({"_id": doc["_id"]}, fixed)
            if result.modified_count or result.matched_count:
                repaired += 1
            if fixed["YSR"] is None:
                dropped += 1
        total = collection.count_documents({})
        print(f"{name}: repaired {repaired}, skipped {skipped}, null YSR values {dropped}, total {total}")
    for name in COLLECTIONS:
        sample = db[name].find_one({}, {"_id": 0})
        keys = sorted(k for k in sample if k != "Date")
        print(f"{name}: {len(keys)} district keys -> {keys}")


if __name__ == "__main__":
    main()
