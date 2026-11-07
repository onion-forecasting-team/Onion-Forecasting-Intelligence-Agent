"""Import a (new) Avaghana static dataset into MongoDB.

Graph 1 serves the Avaghana source entirely from two MongoDB collections:
``Avaghana_AP_Retail_Dataset`` (history) and ``AVAGHANA_AP_FORECAST``
(pre-generated six-month Chronos-2 forecast). The API never regenerates the
Avaghana forecast — refreshing the snapshot means re-importing newer files
with this script.

Expected inputs (CSV, written next to the project data/ folder):
- history CSV: the Avaghana workbook export with a ``source_sheet`` column
  (State Daily Output rows carry ``AP_Avaghana_Retail_State_Average``);
  district/coverage/methodology rows are imported as-is when they can be
  stored, dotted column names (e.g. ``Y.S.R``) block those documents and are
  reported instead of silently renamed.
- forecast CSV: ``Date`` + ``Retail`` columns (optional ``P10``/``P90``).

The script is read-only by default. Pass ``--yes`` to actually replace the
collections (drop + insert), then restart the backend so its per-process
static-data cache reloads.

Usage (from the project root):
    .\\.venv\\Scripts\\python.exe scripts\\import_avaghana.py                 # dry-run report
    .\\.venv\\Scripts\\python.exe scripts\\import_avaghana.py --yes            # import
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "data_layer"))

import pandas as pd  # noqa: E402
from db_client import get_db  # noqa: E402
import history_store  # noqa: E402

HISTORY_COLLECTION = "Avaghana_AP_Retail_Dataset"
FORECAST_COLLECTION = "AVAGHANA_AP_FORECAST"
STATE_PRICE_FIELD = "AP_Avaghana_Retail_State_Average"


def dotted_columns(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if "." in str(c)]


def survey_history(frame: pd.DataFrame) -> dict:
    state = frame[frame.get(STATE_PRICE_FIELD).notna()] if STATE_PRICE_FIELD in frame.columns else frame.iloc[0:0]
    sheets = frame["source_sheet"].value_counts().to_dict() if "source_sheet" in frame.columns else {}
    report: dict = {
        "rows_total": int(len(frame)),
        "rows_per_source_sheet": {k: int(v) for k, v in sheets.items()},
        "state_rows": int(len(state)),
        "dotted_columns": dotted_columns(frame),
        "state_zero_prices": int((pd.to_numeric(state.get(STATE_PRICE_FIELD), errors="coerce") == 0).sum()),
        "state_null_prices": int(pd.to_numeric(state.get(STATE_PRICE_FIELD), errors="coerce").isna().sum()),
    }
    dates = pd.to_datetime(state.get("Date"), errors="coerce").dropna()
    if not dates.empty:
        report["history_start"] = dates.min().date().isoformat()
        report["last_observation"] = dates.max().date().isoformat()
        per_day = dates.dt.date.value_counts()
        report["duplicate_state_dates"] = int((per_day > 1).sum())
        full = pd.Series(pd.date_range(min(dates.max()), max(dates.max()), freq="D"))
        missing = full[~full.dt.date.isin(set(dates.dt.date))]
        report["missing_days"] = int(len(missing))
    return report


def survey_forecast(frame: pd.DataFrame) -> dict:
    dates = pd.to_datetime(frame.get("Date"), errors="coerce").dropna()
    report: dict = {
        "rows": int(len(frame)),
        "null_prices": int(pd.to_numeric(frame.get("Retail"), errors="coerce").isna().sum()),
        "has_quantiles": bool({"P10", "P90"}.issubset(frame.columns)),
        "dotted_columns": dotted_columns(frame),
    }
    if not dates.empty:
        report["forecast_start"] = dates.min().date().isoformat()
        report["forecast_end"] = dates.max().date().isoformat()
        gaps = (dates.diff().dt.days.dropna() != 1).sum()
        report["non_daily_steps"] = int(gaps)
    return report


def unit_crosscheck(frame: pd.DataFrame, last_observation: str | None) -> dict:
    """Compare recent Avaghana state prices with UPAG retail actuals (INR/kg)."""
    result: dict = {"checked": 0, "note": "no overlapping dates with daily_price_observations"}
    if last_observation is None or STATE_PRICE_FIELD not in frame.columns:
        return result
    db = get_db()
    state = frame.dropna(subset=[STATE_PRICE_FIELD, "Date"])
    state = state[pd.to_datetime(state["Date"]) <= pd.Timestamp(last_observation)]
    recent = state.tail(10)
    deltas, samples = [], []
    for _, row in recent.iterrows():
        day = str(pd.Timestamp(row["Date"]).date().isoformat())
        doc = db["daily_price_observations"].find_one({"timestamp": day},
                                                      {history_store.PRICE_COLUMNS["AP"]["retail"]: 1, "_id": 0})
        if not doc:
            continue
        upag = doc.get(history_store.PRICE_COLUMNS["AP"]["retail"])
        if upag is None:
            continue
        upag /= 100.0  # stored in INR/metric quintal -> INR/kg
        avaghana = float(row[STATE_PRICE_FIELD])
        deltas.append(abs(avaghana - upag))
        samples.append((day, round(avaghana, 2), round(upag, 2)))
    result.update({"checked": len(samples), "samples": samples[-5:]})
    if deltas:
        mean_gap = sum(deltas) / len(deltas)
        scale = "same scale (INR/kg plausible)" if mean_gap < 50 else \
                "POTENTIAL UNIT MISMATCH (factor ~100 or worse) — verify INR/kg assumption with the BA team"
        result["mean_abs_gap_vs_upag"] = round(mean_gap, 2)
        result["unit_verdict"] = scale
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the Avaghana static dataset into MongoDB.")
    parser.add_argument("--history-csv", default="data/Avaghana_AP_Retail_Dataset.csv",
                        help="CSV path matching the history collection layout")
    parser.add_argument("--forecast-csv", default="data/AVAGHANA_AP_FORECAST.csv",
                        help="CSV path with Date (+ optional P10/Retail/P90) forecast columns")
    parser.add_argument("--yes", action="store_true",
                        help="Actually replace the two collections (default: read-only survey)")
    args = parser.parse_args()

    history_path = ROOT / args.history_csv
    forecast_path = ROOT / args.forecast_csv
    for path in (history_path, forecast_path):
        if not path.exists():
            print(f"MISSING: {path}")
            sys.exit(1)

    hist = pd.read_csv(history_path)
    fcst = pd.read_csv(forecast_path)
    hist_report = survey_history(hist)
    fcst_report = survey_forecast(fcst)

    print("== Avaghana history survey ==")
    for key, value in hist_report.items():
        print(f"  {key}: {value}")
    print("== Avaghana forecast survey ==")
    for key, value in fcst_report.items():
        print(f"  {key}: {value}")

    issues: list[str] = []
    if hist_report["dotted_columns"]:
        issues.append("history CSV has dotted column(s) "
                      f"{hist_report['dotted_columns']}; flatten/rename them first "
                      "(see scripts/repair_ysr_district.py for the Y.S.R precedent)")
    if fcst_report["dotted_columns"]:
        issues.append("forecast CSV has dotted column(s)")
    if hist_report.get("duplicate_state_dates"):
        issues.append(f"{hist_report['duplicate_state_dates']} duplicated state dates")
    if hist_report.get("missing_days"):
        issues.append(f"{hist_report['missing_days']} missing day(s) in the state series")
    if fcst_report.get("non_daily_steps"):
        issues.append(f"{fcst_report['non_daily_steps']} non-daily step(s) in the forecast dates")
    if not fcst_report["rows"]:
        issues.append("forecast CSV is empty")
    boundary_ok = None
    if hist_report.get("last_observation") and fcst_report.get("forecast_start"):
        next_day = date.fromisoformat(hist_report["last_observation"]) + pd.Timedelta(days=1)
        boundary_ok = fcst_report["forecast_start"] == next_day.isoformat()
        if not boundary_ok:
            issues.append("forecast does not start the day after the last observation "
                          f"({hist_report['last_observation']} -> {fcst_report['forecast_start']})")
    if issues:
        print("\n== Data-quality issues ==")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("\nData-quality checks passed.")
    print(f"Boundary continuity (forecast starts day after last observation): "
          f"{boundary_ok if boundary_ok is not None else 'n/a'}")

    print("\n== Unit cross-check vs UPAG retail (INR/kg) ==")
    for key, value in unit_crosscheck(hist, hist_report.get("last_observation")).items():
        print(f"  {key}: {value}")

    if not args.yes:
        print("\nDry run — pass --yes to replace the collections, then restart the backend.")
        return

    if issues:
        print("\n--yes refused: resolve the data-quality issues above first (or fix the files).")
        sys.exit(2)

    hist_docs = hist.where(pd.notna(hist), None).to_dict(orient="records")
    fcst_docs = fcst.where(pd.notna(fcst), None).to_dict(orient="records")
    try:
        db = get_db()
        db[HISTORY_COLLECTION].drop()
        db[HISTORY_COLLECTION].insert_many(hist_docs)
        db[FORECAST_COLLECTION].drop()
        db[FORECAST_COLLECTION].insert_many(fcst_docs)
        print(f"Imported {len(hist_docs)} history docs -> {HISTORY_COLLECTION}; "
              f"{len(fcst_docs)} forecast docs -> {FORECAST_COLLECTION}")
        print("Restart the backend so its per-process Avaghana cache reloads: "
              ".venv\\Scripts\\python.exe src\\api\\backend.py")
    except Exception:
        print("Import failed before completion — drop and retry; check server output above.")
        raise


if __name__ == "__main__":
    main()
