"""One-time ops script: regenerate the missing model-ready seed CSV.

`config/.env`'s ONION_DATA_PATH (default Onion_Model_Ready_Dataset_Chronos2_
2026-10-26_Interpolated_Metric_Quintal.csv) is the seed CSV that
history_store.ensure_store_ready() uses when a fresh store is empty (fresh
installs, tests). This machine's copy went missing; the retained SQLite
backup's observations table holds exactly the seeded rows, so export it back.

Columns follow history_store.EXPECTED_COLUMNS order, values serialized with
full float precision, UTF-8 without BOM.

  .venv\\Scripts\\python.exe scripts\\export_observations_csv.py
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
import history_store  # noqa: E402


def main() -> int:
    conn = sqlite3.connect(history_store.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM observations ORDER BY timestamp").fetchall()
    conn.close()
    if not rows:
        print("SQLite observations table is empty; nothing to export.", file=sys.stderr)
        return 1

    import pandas as pd

    frame = pd.DataFrame([dict(row) for row in rows])
    columns = [c for c in history_store.EXPECTED_COLUMNS if c in frame.columns]
    frame = frame[columns]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.strftime("%Y-%m-%d")
    out_path = history_store.CSV_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_path, index=False, float_format="%.17g")
    print(json.dumps({"rows": int(len(frame)), "columns": list(frame.columns),
                      "target": str(out_path)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
