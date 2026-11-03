"""Merge an updated, combined model-ready CSV into the local SQLite history.

Example:
  python sync_model_ready_csv.py data/Onion_Model_Ready_Dataset_Chronos2.csv

The file must have all expected columns for AP/KA/MH/MP with one row per day.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from history_store import ensure_store_ready, sync_model_ready_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Path to the updated combined model-ready CSV")
    parser.add_argument("--source", default="manual_model_ready_csv", help="Audit label for this import")
    args = parser.parse_args()
    try:
        ensure_store_ready()
        result = sync_model_ready_csv(Path(args.csv_path), source=args.source)
        print(json.dumps({"status": "success", **result.as_dict()}, indent=2))
        return 0
    except Exception as exc:
        print(f"SYNC FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
