"""One-time ops script: align MongoDB collection names with the code schema.

The migration plan initially introduced renamed collections:
    daily_prices      (code expects daily_price_observations)
    monthly_balances  (code expects ap_monthly_observations)
    ingestion_logs    (code expects  ingestion_log)
The application keeps the former SQLite table names so every pandas/API path
behaves identically. This script renames any plan-style aliases to the
canonical names on the connected database (server-side rename, instant,
idempotent — it never touches data documents).

  .venv\\Scripts\\python.exe scripts\\align_collection_names.py
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

ALIASES = {
    "daily_prices": "daily_price_observations",
    "monthly_balances": "ap_monthly_observations",
    "ingestion_logs": "ingestion_log",
}


def main() -> int:
    db = db_client.get_db()
    existing = set(db.list_collection_names())
    actions: list[str] = []
    warnings: list[str] = []

    for alias, canonical in ALIASES.items():
        has_alias = alias in existing
        has_canonical = canonical in existing
        if has_alias and has_canonical:
            warnings.append(
                f"Both '{alias}' and '{canonical}' exist. NOT renaming automatically — "
                f"inspect which one holds live data (e.g. counts) and merge manually.")
        elif has_alias and not has_canonical:
            db.command("renameCollection", f"{db.name}.{alias}",
                       to=f"{db.name}.{canonical}")
            actions.append(f"renamed {alias} -> {canonical}")
        elif not has_alias and not has_canonical:
            actions.append(f"neither '{alias}' nor '{canonical}' present; the first "
                           f"dataset sync will create '{canonical}'")

    print(json.dumps({
        "database": db.name,
        "collections_before": sorted(existing),
        "collections_after": sorted(db.list_collection_names()),
        "actions": actions,
        "warnings": warnings,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
