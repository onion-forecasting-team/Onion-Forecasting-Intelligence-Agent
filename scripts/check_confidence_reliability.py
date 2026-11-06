"""Reliability check for the derived per-day confidence score.

Compares the displayed tolerance-band confidence against realized outcomes:
for every persisted Graph 1/2 price forecast row whose forecast date already has
an observed actual in MongoDB, the script recomputes the score exactly as the
API does (quantized values, per-series spread multiplier k) and checks whether
the actual fell inside the +/- ONION_CONFIDENCE_TOLERANCE_PCT band around the
median. Realized hit-rate per score bucket is then compared with the mean
displayed score in that bucket, so overconfidence (displayed >> realized) shows
up as a negative realized-minus-displayed gap.

Notes:
- Scores are reconstructed per row without the API's cumulative-minimum
  presentation rule, so they measure the raw per-day score.
- Only the latest run's row is used per (state, series, forecast date); older
  duplicate runs for the same date do not inflate the sample.
- Read-only: nothing is written to MongoDB or the data files.

Usage (from the project root):
    .\\.venv\\Scripts\\python.exe scripts\\check_confidence_reliability.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date as date_type
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "data_layer"))

import main  # noqa: E402  (loads config/.env; pre-warm only runs on server startup)
import history_store  # noqa: E402
from db_client import get_db  # noqa: E402

PRICE_SERIES = ("retail", "wholesale", "mandi_modal")
PRICE_GRAPH_CODES = ("graph1_ap_prices", "graph2_state_comparison")


def load_actuals(db) -> dict[tuple[str, str, str], float]:
    """Actual state-level prices in INR/kg keyed by (state, series, date)."""
    actuals: dict[tuple[str, str, str], float] = {}
    for doc in db["daily_price_observations"].find({}, {"_id": 0}):
        stamp = doc.get("timestamp")
        if not stamp:
            continue
        day = str(stamp)[:10]
        for state in history_store.STATE_CODES:
            for series in PRICE_SERIES:
                value = doc.get(history_store.PRICE_COLUMNS[state][series])
                if value is not None:
                    actuals[(state, series, day)] = float(value) / 100.0
    return actuals


def load_displayed_rows(db, graph_codes: tuple[str, ...]) -> dict[tuple[str, str, str], dict]:
    """Latest rewrite for every realized forecast key across the price runs."""
    runs = list(db["forecast_runs"].find(
        {"graph_code": {"$in": list(graph_codes)}}, {"_id": 0}))
    runs.sort(key=lambda r: (int(r.get("run_id", 0))))  # descending rewrite order below
    rows: dict[tuple[str, str, str], dict] = {}
    for run in runs:
        last_observed = run.get("last_observation_date")
        effective_horizon = run.get("horizon_days")
        for value in db["forecast_values"].find(
                {"run_id": int(run["run_id"]),
                 "target_key": {"$in": list(PRICE_SERIES)}, "unit": "INR/kg"},
                {"_id": 0}):
            key = (value["state_code"], value["target_key"], str(value["forecast_date"])[:10])
            row = {
                "p10": float(value["p10"]), "p50": float(value["p50"]),
                "p90": float(value["p90"]), "last_observed": last_observed,
                "effective_horizon": effective_horizon, "run_id": int(run["run_id"]),
            }
            # later runs overwrite earlier ones (latest rewrite wins)
            rows[key] = row
    return rows


def displayed_score(series: str, p10: float, p50: float, p90: float) -> float:
    """Reproduce the API's per-row score: quantize to the stored 4 decimals,
    convert back to model units, and score with the series' calibrated k."""
    q10, q50, q90 = (round(v, 4) * 100.0 for v in (p10, p50, p90))
    return main._confidence_score(q10, q50, q90, main.CONFIDENCE_SPREAD_K_BY_PRICE_TYPE[series])


def bucket_of(score: float, width: float) -> int:
    return min(int(score / width), int(100.0 / width) - 1)


def evaluate(rows: dict, actuals: dict, tolerance: float) -> list[dict]:
    samples = []
    for (state, series, day), row in rows.items():
        actual = actuals.get((state, series, day))
        if actual is None:
            continue
        p50 = row["p50"]
        hit = abs(actual - p50) <= tolerance * abs(p50)
        effective_days = None
        if row["last_observed"]:
            try:
                effective_days = (date_type.fromisoformat(day)
                                  - date_type.fromisoformat(row["last_observed"])).days
            except ValueError:
                effective_days = None
        samples.append({
            "series": series, "state": state, "date": day,
            "score": displayed_score(series, row["p10"], p50, row["p90"]),
            "hit": hit, "effective_horizon": row["effective_horizon"],
            "effective_days": effective_days, "run_id": row["run_id"],
        })
    return samples


def summarize(samples: list[dict], label: str) -> dict:
    if not samples:
        return {"label": label, "n": 0}
    return {
        "label": label, "n": len(samples),
        "mean_displayed": sum(s["score"] for s in samples) / len(samples),
        "realized_hit_pct": 100.0 * sum(1 for s in samples if s["hit"]) / len(samples),
    }


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)] if rows else [len(h) for h in headers]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket-width", type=float, default=10.0,
                        help="Score bucket width in points (default 10).")
    parser.add_argument("--min-bucket-n", type=int, default=20,
                        help="Minimum sample size before a bucket is flagged (default 20).")
    parser.add_argument("--warn-gap-pp", type=float, default=5.0,
                        help="Flag when realized is below displayed by more than this many "
                             "percentage points (default 5).")
    parser.add_argument("--horizon-cap", type=int, default=main.MAX_FORECAST_HORIZON_DAYS,
                        help="Ignore forecasts beyond this many days (API cap, default 180).")
    args = parser.parse_args()

    tolerance = main.CONFIDENCE_TOLERANCE_PCT
    db = get_db()
    actuals = load_actuals(db)
    rows = load_displayed_rows(db, PRICE_GRAPH_CODES)
    samples = evaluate(rows, actuals, tolerance)

    if not samples:
        print("No realized forecast rows found: no persisted Graph 1/2 price forecast dates "
              "overlap an observed actual date yet.")
        return

    print(f"tolerance=±{tolerance * 100:.0f}%  k={main.CONFIDENCE_SPREAD_K_BY_PRICE_TYPE}  "
          f"horizon_cap<= {args.horizon_cap}d")
    print(f"realized rows: {len(samples)} (of {len(rows)} persisted latest-rewrite rows)")

    series_rows = []
    for series in PRICE_SERIES:
        subset = [s for s in samples if s["series"] == series
                  and (s["effective_days"] is None or s["effective_days"] <= args.horizon_cap)]
        s = summarize(subset, series)
        gap = (s["realized_hit_pct"] - s["mean_displayed"]) if s["n"] else None
        series_rows.append([
            series, str(s["n"]),
            f"{s.get('mean_displayed', 0):.1f}", f"{s.get('realized_hit_pct', 0):.1f}",
            f"{gap:+.1f}" if gap is not None else "-",
        ])
    print("\nPer series (displayed score vs realized +/- band hit-rate):")
    print_table(["series", "n", "mean_score", "realized_pct", "gap_pp"], series_rows)

    print("\nScore buckets (all series pooled, realized hit-rate vs mean displayed score):")
    headers = ["score_bin", "n", "mean_score", "realized_pct", "gap_pp", "status"]
    bucket_rows = []
    flagged = 0
    for start in range(0, 100, int(args.bucket_width)):
        lo, hi = start, start + args.bucket_width
        subset = [s for s in samples
                  if lo <= s["score"] < hi
                  and (s["effective_days"] is None or s["effective_days"] <= args.horizon_cap)]
        s = summarize(subset, f"{lo}-{hi}")
        if s["n"] == 0:
            continue
        gap = s["realized_hit_pct"] - s["mean_displayed"]
        status = "ok"
        if s["n"] >= args.min_bucket_n and gap < -args.warn_gap_pp:
            status = "DRIFT"
            flagged += 1
        bucket_rows.append([f"{s['label']}", str(s["n"]), f"{s['mean_displayed']:.1f}",
                            f"{s['realized_hit_pct']:.1f}", f"{gap:+.1f}", status])
    print_table(headers, bucket_rows)

    if flagged:
        print(f"\nWARNING: {flagged} bucket(s) show realized hit-rate more than "
              f"{args.warn_gap_pp}pp below the displayed score (n >= {args.min_bucket_n}). "
              "Recalibrate k before trusting the displayed confidence.")
    else:
        print("\nNo bucket shows material overconfidence "
              f"(threshold {args.warn_gap_pp}pp, n >= {args.min_bucket_n}).")


if __name__ == "__main__":
    main()
