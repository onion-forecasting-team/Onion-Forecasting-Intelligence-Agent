"""MongoDB dataset and forecast persistence for the onion Chronos-2 MVP.

This is the MongoDB architecture port of the former SQLite store. Each former
table is now a collection that keeps the SAME field names, so all pandas and
API behaviour is unchanged:

- `observations`            : full legacy daily observation store (prices INR/metric quintal,
                              arrivals metric quintals/day, quantities metric quintals/day)
- `daily_price_observations`: daily graph 1/2 model input (model units)
- `ap_monthly_observations` : monthly AP production/consumption/arrivals (tonnes/month)
- `forecast_runs` + `forecast_values` : persisted predictions (prices in INR/kg)
- `ingestion_log`           : audit rows for every sync
- `source_sync_state` / `graph_dataset_sync_state` : source-file mtime markers

Graph-specific collections store daily price/arrival observations in model units and AP
monthly production, consumption, and arrivals in tonnes/month. Forecast prices are
persisted separately in INR/kg.

Document `_id` mirrors the former SQL primary keys:
  observations/daily_price_observations -> date ISO string
  ap_monthly_observations               -> month_start ISO string
  chat_sessions/chat forecasts keyed explicitly
Integer sequences (forecast run ids, audit ids, chat message seq) come from the
`counters` collection (atomic $inc), mirroring SQLite AUTOINCREMENT.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from db_client import (COUNTER_IDS, SCHEMA_VERSION, ensure_indexes, get_db,
                       next_sequence, store_label)

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / "config" / ".env")
DEFAULT_CSV = ROOT / "data" / "Onion_Model_Ready_Dataset_Chronos2_2026-10-26_Interpolated_Metric_Quintal.csv"
DEFAULT_DAILY_PRICE_CSV = ROOT / "data" / "Onion_Daily_Price_Forecasting_Dataset_2022-01-01_to_2026-10-25.csv"
DEFAULT_AP_MONTHLY_CSV = ROOT / "data" / "Onion_AP_Monthly_Dataset_with_Prices_2022-01_to_2026-10.csv"
STATE_CODES = ("AP", "KA", "MH", "MP")
STATE_NAMES = {"AP": "Andhra Pradesh", "KA": "Karnataka", "MH": "Maharashtra", "MP": "Madhya Pradesh"}

PRICE_COLUMNS = {
    state: {
        "retail": f"{state}_retail_rs_per_metric_quintal",
        "wholesale": f"{state}_wholesale_rs_per_metric_quintal",
        "mandi_modal": f"{state}_mandi_modal_rs_per_metric_quintal",
    } for state in STATE_CODES
}
QUANTITY_COLUMNS = {
    state: {
        "arrival": f"{state}_arrival_metric_quintal",
        "production": f"{state}_production_metric_quintal_per_day",
        "consumption": f"{state}_consumption_metric_quintal_per_day",
    } for state in STATE_CODES
}
SUPPLY_COLUMNS = {state: f"{state}_supply_metric_quintal_per_day" for state in STATE_CODES}
EXPECTED_COLUMNS = ["item_id", "timestamp"]
for _state in STATE_CODES:
    EXPECTED_COLUMNS.extend(PRICE_COLUMNS[_state][m] for m in ("retail", "wholesale", "mandi_modal"))
    EXPECTED_COLUMNS.extend(QUANTITY_COLUMNS[_state][m] for m in ("arrival", "production", "consumption"))
NUMERIC_COLUMNS = [c for c in EXPECTED_COLUMNS if c not in ("item_id", "timestamp")]


def _resolve_path(env_name: str, fallback: Path) -> Path:
    raw = os.getenv(env_name)
    if not raw:
        return fallback
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()

CSV_PATH = _resolve_path("ONION_DATA_PATH", DEFAULT_CSV)
DAILY_PRICE_CSV_PATH = _resolve_path("ONION_DAILY_PRICE_DATA_PATH", DEFAULT_DAILY_PRICE_CSV)
AP_MONTHLY_CSV_PATH = _resolve_path("ONION_AP_MONTHLY_DATA_PATH", DEFAULT_AP_MONTHLY_CSV)
# Kept for compatibility: points at the retained SQLite backup used by
# scripts/verify_migration.py; the live store is MongoDB (see store_label()).
DB_PATH = _resolve_path("ONION_DB_PATH", ROOT / "data" / "onion_forecasting_v4.sqlite3")

DAILY_GRAPH_COLUMNS = [
    "item_id", "timestamp",
    *(PRICE_COLUMNS[state][measure] for state in STATE_CODES for measure in ("retail", "wholesale", "mandi_modal")),
    *(QUANTITY_COLUMNS[state]["arrival"] for state in STATE_CODES),
]
MONTHLY_GRAPH_COLUMNS = [
    "month_start", "production_t_month", "consumption_t_month", "arrival_t_month",
]

TOLERANCE = 1e-6
DATE_TIME_FORMAT = "%Y-%m-%d"


@dataclass
class SyncResult:
    source: str
    rows_seen: int
    inserted: int
    updated: int
    unchanged: int
    latest_observation_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "rows_seen": self.rows_seen, "inserted": self.inserted,
                "updated": self.updated, "unchanged": self.unchanged,
                "latest_observation_date": self.latest_observation_date}


@dataclass
class DashUpsertResult:
    source: str
    rows_seen: int
    inserted: int
    updated: int
    unchanged: int
    gap_filled: int = 0
    correction_count: int = 0
    latest_observation_date: str | None = None
    corrections: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "rows_seen": self.rows_seen, "inserted": self.inserted,
                "updated": self.updated, "unchanged": self.unchanged, "gap_filled": self.gap_filled,
                "corrections": self.correction_count,
                "latest_observation_date": self.latest_observation_date}


def _canonicalize_source_columns(frame: pd.DataFrame) -> pd.DataFrame:
    src = frame.copy()
    src.columns = [str(c).replace("\ufeff", "").strip() for c in src.columns]
    rename = {}
    for state in STATE_CODES:
        # Price column alternatives, including the older MVP's INR/kg columns.
        for measure in ("retail", "wholesale", "mandi_modal"):
            target = PRICE_COLUMNS[state][measure]
            alternatives = [
                target,
                f"{state}_{measure}_rs_per_metric_quintal",
                f"{state}_{measure}_rs_kg",
                f"{state}_{measure}_rs_kg" if measure != "mandi_modal" else f"{state}_mandi_modal_rs_kg",
            ]
            for candidate in alternatives:
                if candidate in src.columns:
                    rename[candidate] = target
                    break
        # Quantity alternatives.
        quantity_alts = {
            "arrival": [QUANTITY_COLUMNS[state]["arrival"], f"{state}_arrival_mq", f"{state}_arrival_t",
                        f"{state}_arrival_t_day"],
            "production": [QUANTITY_COLUMNS[state]["production"], f"{state}_production_mq_day", f"{state}_production_t_day"],
            "consumption": [QUANTITY_COLUMNS[state]["consumption"], f"{state}_consumption_mq_day", f"{state}_consumption_t_day"],
        }
        for measure, candidates in quantity_alts.items():
            for candidate in candidates:
                if candidate in src.columns:
                    rename[candidate] = QUANTITY_COLUMNS[state][measure]
                    break
    src = src.rename(columns=rename)
    return src


def _validate_graph_frame(out: pd.DataFrame, date_column: str, frequency: str) -> pd.DataFrame:
    if out.empty:
        raise ValueError("Dataset must contain at least one observation.")
    if out[date_column].isna().any():
        raise ValueError(f"Dataset contains invalid or blank {date_column} values.")
    if out[date_column].duplicated().any():
        raise ValueError(f"Dataset contains duplicate {date_column} values.")
    for column in out.columns:
        if column in (date_column, "item_id"):
            continue
        out[column] = pd.to_numeric(out[column], errors="coerce")
    numeric = [column for column in out.columns if column not in (date_column, "item_id")]
    bad = [column for column in numeric if out[column].isna().any()
           or not np.isfinite(out[column].to_numpy(dtype=float)).all()]
    if bad:
        raise ValueError("Dataset has missing/non-numeric/non-finite values in: " + ", ".join(bad))
    out = out.sort_values(date_column).reset_index(drop=True)
    if frequency == "D":
        expected = pd.date_range(out[date_column].min(), out[date_column].max(), freq="D")
        observed = pd.DatetimeIndex(out[date_column])
    else:
        expected = pd.date_range(out[date_column].min(), out[date_column].max(), freq="MS")
        observed = pd.DatetimeIndex(out[date_column])
    if not observed.equals(expected):
        raise ValueError(f"Dataset must have a continuous {frequency} date index.")
    return out


def normalize_daily_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize the new daily market dataset to graph 1/2 model columns."""
    original = frame.copy()
    original.columns = [str(c).replace("\ufeff", "").strip() for c in original.columns]
    if "timestamp" not in original.columns and "date" in original.columns:
        original = original.rename(columns={"date": "timestamp"})
    src = _canonicalize_source_columns(original)
    if "timestamp" not in src.columns:
        raise ValueError("Daily price dataset must include a 'date' or 'timestamp' column.")

    out = pd.DataFrame()
    out["timestamp"] = pd.to_datetime(src["timestamp"], errors="coerce").dt.normalize()
    out["item_id"] = src["item_id"].astype(str) if "item_id" in src.columns else "AP_onion"
    for state in STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            canonical = PRICE_COLUMNS[state][measure]
            if canonical not in src.columns:
                raise ValueError(f"Missing daily price column for {state} {measure}.")
            values = pd.to_numeric(src[canonical], errors="coerce")
            old_name = f"{state}_{measure}_rs_kg"
            if measure == "mandi_modal":
                old_name = f"{state}_mandi_modal_rs_kg"
            if old_name in original.columns and canonical not in original.columns:
                values = values * 100.0
            out[canonical] = values

        arrival = QUANTITY_COLUMNS[state]["arrival"]
        if arrival not in src.columns:
            raise ValueError(f"Missing daily arrival column for {state}.")
        values = pd.to_numeric(src[arrival], errors="coerce")
        if f"{state}_arrival_t_day" in original.columns and arrival not in original.columns:
            values = values * 10.0
        out[arrival] = values

    if out["item_id"].nunique() != 1:
        raise ValueError("Daily graph dataset must contain one item_id and one aligned row per date.")
    out["item_id"] = out["item_id"].astype(str)
    return _validate_graph_frame(out[DAILY_GRAPH_COLUMNS], "timestamp", "D")


MONTHLY_RETAIL_PRICE_ALIASES = {
    "retail_price_rs_kg": (
        "AP_avg_retail_price_rs_kg", "avg_retail_price_rs_kg",
        "retail_price_rs_kg", "AP_retail_rs_kg"),
    "wholesale_price_rs_kg": (
        "AP_avg_wholesale_price_rs_kg", "avg_wholesale_price_rs_kg",
        "wholesale_price_rs_kg", "AP_wholesale_rs_kg"),
    "mandi_price_rs_kg": (
        "AP_avg_mandi_modal_rs_kg", "avg_mandi_price_rs_kg",
        "mandi_price_rs_kg", "AP_mandi_modal_rs_kg"),
}


def normalize_ap_monthly_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize monthly AP quantities, preserving their source tonnes/month units.

    Optional monthly price columns are canonicalized to retail_price_rs_kg,
    wholesale_price_rs_kg and mandi_price_rs_kg when any alias is present.
    """
    src = frame.copy()
    src.columns = [str(c).replace("\ufeff", "").strip() for c in src.columns]
    if "month_start" not in src.columns:
        raise ValueError("Monthly AP dataset must include a 'month_start' column.")
    required = MONTHLY_GRAPH_COLUMNS[1:]
    missing = [column for column in required if column not in src.columns]
    if missing:
        raise ValueError("Monthly AP dataset is missing columns: " + ", ".join(missing))
    parsed = pd.to_datetime(src["month_start"], format="%b-%y", errors="coerce")
    invalid = parsed.isna()
    if invalid.any():
        parsed.loc[invalid] = pd.to_datetime(src.loc[invalid, "month_start"], errors="coerce")

    out = pd.DataFrame({
        "month_start": parsed.dt.to_period("M").dt.to_timestamp(),
        **{column: src[column] for column in required},
    })
    for canonical, aliases in MONTHLY_RETAIL_PRICE_ALIASES.items():
        for alias in aliases:
            if alias in src.columns:
                out[canonical] = src[alias]
                break
    columns = ["month_start", *required]
    columns.extend(
        canonical for canonical in MONTHLY_RETAIL_PRICE_ALIASES
        if canonical in out.columns)
    return _validate_graph_frame(out[columns], "month_start", "MS")


# ---------------------------------------------------------------------------
# MongoDB graph-dataset sync (mtime-tracked upsert, replaces _sync_graph_dataset)
# ---------------------------------------------------------------------------

def _sync_graph_dataset(dataset_key: str, path: Path, collection: str, frame: pd.DataFrame,
                        date_column: str) -> None:
    stat = path.stat()
    resolved = str(path.resolve())
    db = get_db()
    ensure_indexes(db)
    state = db.graph_dataset_sync_state.find_one({"dataset_key": dataset_key})
    if (state is not None
            and state.get("source_path") == resolved
            and int(state.get("source_mtime_ns", -1)) == int(stat.st_mtime_ns)
            and int(state.get("source_size", -1)) == int(stat.st_size)):
        return

    records = []
    for row in frame.itertuples(index=False, name=None):
        values = dict(zip(frame.columns, row))
        stamp = pd.Timestamp(values[date_column]).strftime(DATE_TIME_FORMAT)
        values[date_column] = stamp
        # BSON only encodes native Python types; pandas int64 columns would raise
        # InvalidDocument. Every numeric column is validated float-able above.
        plain = {}
        for key, value in values.items():
            if key in (date_column, "item_id", "timestamp"):
                plain[key] = str(value)
            else:
                plain[key] = float(value)
        plain["version"] = SCHEMA_VERSION
        records.append({"_id": stamp, **plain})
    if records:
        upserts = []
        for record in records:
            plain = {k: v for k, v in record.items() if k != "_id"}
            upserts.append(({"_id": record["_id"]}, plain))
        for doc_id, plain in upserts:
            db[collection].replace_one(doc_id, plain, upsert=True)
    db.graph_dataset_sync_state.update_one(
        {"dataset_key": dataset_key},
        {"$set": {
            "dataset_key": dataset_key,
            "source_path": resolved,
            "source_mtime_ns": int(stat.st_mtime_ns),
            "source_size": int(stat.st_size),
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }},
        upsert=True,
    )


def _collection_rows(db, collection: str, columns: list[str], date_column: str) -> pd.DataFrame:
    cursor = db[collection].find(
        {},
        {column: 1 for column in columns} | {"_id": 0},
        sort=[(date_column, 1)],
    )
    return pd.DataFrame(list(cursor), columns=columns)


def load_daily_price_history() -> pd.DataFrame:
    if not DAILY_PRICE_CSV_PATH.exists():
        raise FileNotFoundError(f"Daily price dataset not found: {DAILY_PRICE_CSV_PATH}")
    frame = normalize_daily_price_frame(
        pd.read_csv(DAILY_PRICE_CSV_PATH, encoding="utf-8-sig"))
    _sync_graph_dataset("daily_prices", DAILY_PRICE_CSV_PATH,
                        "daily_price_observations", frame, "timestamp")
    db = get_db()
    stored = _collection_rows(db, "daily_price_observations", DAILY_GRAPH_COLUMNS, "timestamp")
    stored["timestamp"] = pd.to_datetime(stored["timestamp"])
    return stored


def sync_daily_price_csv_from_graph(dates: Iterable[str]) -> dict[str, int]:
    """Persist selected MongoDB daily graph rows back to the configured source CSV."""
    stamps = sorted({str(stamp) for stamp in dates})
    result = {"rows_appended": 0, "values_updated": 0}
    if not stamps:
        return result

    path = DAILY_PRICE_CSV_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Daily price dataset not found: {path}")
    source = pd.read_csv(path, encoding="utf-8-sig")
    date_column = "date" if "date" in source.columns else "timestamp"
    if date_column not in source.columns:
        raise ValueError("Daily price dataset must include a 'date' or 'timestamp' column.")
    source_dates = pd.to_datetime(source[date_column], errors="coerce").dt.normalize()
    if source_dates.isna().any() or source_dates.duplicated().any():
        raise ValueError("Daily price dataset contains invalid or duplicate dates.")
    normalize_daily_price_frame(source)

    db = get_db()
    graph = _collection_rows(db, "daily_price_observations", DAILY_GRAPH_COLUMNS, "timestamp")
    graph["timestamp"] = pd.to_datetime(graph["timestamp"]).dt.normalize()
    graph_by_date = {
        row["timestamp"].strftime("%Y-%m-%d"): row
        for row in graph.to_dict(orient="records")
    }

    column_map: dict[str, tuple[str, float]] = {}
    for state in STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            canonical = PRICE_COLUMNS[state][measure]
            legacy = (f"{state}_{measure}_rs_kg" if measure != "mandi_modal"
                      else f"{state}_mandi_modal_rs_kg")
            if canonical in source.columns:
                column_map[canonical] = (canonical, 1.0)
            elif legacy in source.columns:
                column_map[canonical] = (legacy, 100.0)
            else:
                raise ValueError(f"Daily price dataset is missing column {canonical}.")

        canonical_arrival = QUANTITY_COLUMNS[state]["arrival"]
        arrival_candidates = (
            (canonical_arrival, 1.0),
            (f"{state}_arrival_mq", 1.0),
            (f"{state}_arrival_t", 1.0),
            (f"{state}_arrival_t_day", 10.0),
        )
        for column, scale in arrival_candidates:
            if column in source.columns:
                column_map[canonical_arrival] = (column, scale)
                break
        else:
            raise ValueError(f"Daily price dataset is missing column {canonical_arrival}.")

    source_index = {
        stamp.strftime("%Y-%m-%d"): index
        for index, stamp in enumerate(source_dates)
    }
    updated = source.copy()
    new_rows = []
    date_sample = str(source.iloc[0][date_column])
    for stamp in stamps:
        graph_row = graph_by_date.get(stamp)
        if graph_row is None:
            raise ValueError(f"MongoDB daily graph history is missing {stamp}.")

        index = source_index.get(stamp)
        if index is None:
            date = pd.Timestamp(stamp).date()
            row = {column: pd.NA for column in source.columns}
            row[date_column] = (f"{date.month}/{date.day}/{date.year}"
                                if "/" in date_sample else date.isoformat())
            if "item_id" in row:
                row["item_id"] = graph_row["item_id"]
            for canonical, (source_column, scale) in column_map.items():
                row[source_column] = float(graph_row[canonical]) / scale
            new_rows.append(row)
            result["rows_appended"] += 1
            continue

        for canonical, (source_column, scale) in column_map.items():
            value = float(graph_row[canonical]) / scale
            old_value = pd.to_numeric(
                pd.Series([updated.at[index, source_column]]), errors="coerce"
            ).iloc[0]
            if pd.isna(old_value) or not math.isclose(
                    float(old_value), value, rel_tol=0.0, abs_tol=1e-12):
                updated.at[index, source_column] = value
                result["values_updated"] += 1
        if "item_id" in updated.columns:
            updated.at[index, "item_id"] = graph_row["item_id"]

    if new_rows:
        updated = pd.concat([updated, pd.DataFrame(new_rows, columns=source.columns)], ignore_index=True)
    sort_dates = pd.to_datetime(updated[date_column], errors="coerce")
    if sort_dates.isna().any():
        raise ValueError("Daily price dataset contains invalid dates after synchronization.")
    updated = updated.loc[sort_dates.sort_values(kind="stable").index].reset_index(drop=True)
    normalize_daily_price_frame(updated)
    if updated.equals(source):
        return result

    temporary_path = path.with_name(path.name + ".tmp")
    try:
        updated.to_csv(temporary_path, index=False)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return result


def load_ap_monthly_history() -> pd.DataFrame:
    """Load monthly AP observations, preferring MongoDB datasets over CSV.

    Source order:
    1. MongoDB ``Monthly retail`` collection (monthly retail price dataset).
    2. MongoDB ``ap_monthly_observations`` (existing monthly graph dataset).
    3. Configured CSV, which is synchronized into ``ap_monthly_observations``
       when neither MongoDB dataset has rows.
    """
    db = get_db()

    monthly_retail_docs = []
    try:
        monthly_retail_docs = list(db["Monthly retail"].find({}, {"_id": 0}))
    except Exception:
        monthly_retail_docs = []
    if monthly_retail_docs:
        frame = normalize_ap_monthly_frame(pd.DataFrame(monthly_retail_docs))
        if "month_start" in frame.columns:
            frame["month_start"] = pd.to_datetime(frame["month_start"])
        return frame

    stored = pd.DataFrame(list(
        db["ap_monthly_observations"].find({}, {"_id": 0}).sort("month_start", 1)))
    if stored.shape[0] and "month_start" in stored.columns:
        stored["month_start"] = pd.to_datetime(stored["month_start"])
        return normalize_ap_monthly_frame(stored)

    if not AP_MONTHLY_CSV_PATH.exists():
        raise FileNotFoundError(f"Monthly AP dataset not found: {AP_MONTHLY_CSV_PATH}")
    frame = normalize_ap_monthly_frame(
        pd.read_csv(AP_MONTHLY_CSV_PATH, encoding="utf-8-sig"))
    _sync_graph_dataset("ap_monthly", AP_MONTHLY_CSV_PATH,
                        "ap_monthly_observations", frame, "month_start")
    return frame


def load_monthly_retail_history() -> pd.DataFrame:
    """Load Graph 4 observations directly from MongoDB's Monthly retail collection."""
    db = get_db()
    documents = list(db["Monthly retail"].find({}, {"_id": 0}))
    if not documents:
        raise ValueError("MongoDB collection 'Monthly retail' contains no observations.")
    return normalize_ap_monthly_frame(pd.DataFrame(documents))


def ensure_graph_datasets_ready() -> None:
    """Import the configured graph datasets when their MongoDB collections are empty."""
    db = get_db()
    ensure_indexes(db)
    daily_count = int(db["daily_price_observations"].count_documents({}))
    monthly_count = int(db["ap_monthly_observations"].count_documents({}))
    if daily_count == 0:
        load_daily_price_history()
    if monthly_count == 0:
        load_ap_monthly_history()


def normalize_source_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize schema to DB's canonical columns and units (prices ₹/metric quintal).

    The cleaned standardized file already uses ₹/metric quintal and metric quintals.
    Legacy *_rs_kg prices are multiplied by 100; legacy tonne quantities are multiplied
    by 10. Unit conversions are only done when the input column name indicates the old unit.
    """
    original = frame.copy()
    original.columns = [str(c).replace("\ufeff", "").strip() for c in original.columns]
    src = _canonicalize_source_columns(original)
    if "timestamp" not in src.columns:
        raise ValueError("Dataset must include a 'timestamp' date column.")
    out = pd.DataFrame()
    out["timestamp"] = pd.to_datetime(src["timestamp"], errors="coerce")
    if out["timestamp"].isna().any():
        raise ValueError("Input contains invalid or blank timestamps.")
    out["timestamp"] = out["timestamp"].dt.normalize()
    out["item_id"] = src["item_id"].astype(str) if "item_id" in src.columns else "AP_onion"

    for state in STATE_CODES:
        for measure in ("retail", "wholesale", "mandi_modal"):
            canonical = PRICE_COLUMNS[state][measure]
            if canonical not in src.columns:
                raise ValueError(f"Missing price column for {state} {measure}: expected {canonical}.")
            vals = pd.to_numeric(src[canonical], errors="coerce")
            # If source was the legacy model-ready file, its old name means ₹/kg.
            old_name = f"{state}_{measure}_rs_kg"
            if measure == "mandi_modal":
                old_name = f"{state}_mandi_modal_rs_kg"
            if old_name in original.columns and canonical not in original.columns:
                vals = vals * 100.0
            out[canonical] = vals
        for measure in ("arrival", "production", "consumption"):
            canonical = QUANTITY_COLUMNS[state][measure]
            if canonical not in src.columns:
                raise ValueError(f"Missing quantity column for {state} {measure}: expected {canonical}.")
            vals = pd.to_numeric(src[canonical], errors="coerce")
            legacy = f"{state}_{measure}_t" if measure == "arrival" else f"{state}_{measure}_t_day"
            if legacy in original.columns and canonical not in original.columns:
                vals = vals * 10.0
            out[canonical] = vals

    out = out[EXPECTED_COLUMNS]
    if out["timestamp"].duplicated().any():
        dates = out.loc[out["timestamp"].duplicated(keep=False), "timestamp"].dt.strftime("%Y-%m-%d").head(4).tolist()
        raise ValueError(f"Input contains duplicate dates, e.g. {dates}.")
    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    bad = [c for c in NUMERIC_COLUMNS if out[c].isna().any() or not np.isfinite(out[c].to_numpy(dtype=float)).all()]
    if bad:
        raise ValueError("Input has missing/non-numeric/non-finite values in: " + ", ".join(bad))
    if out["item_id"].nunique() != 1:
        raise ValueError("Combined dataset must contain one item_id and one aligned row per date.")
    out["item_id"] = out["item_id"].astype(str)
    out = out.sort_values("timestamp").reset_index(drop=True)
    expected = pd.date_range(out["timestamp"].min(), out["timestamp"].max(), freq="D")
    if not pd.DatetimeIndex(out["timestamp"]).equals(expected):
        raise ValueError("Dataset must have a continuous daily date index.")
    return out


# ---------------------------------------------------------------------------
# Ingestion audit log
# ---------------------------------------------------------------------------

def _log_sync(result: SyncResult, status: str = "success", message: str | None = None) -> dict[str, Any]:
    """Insert one audit row with a sequential id (SQLite AUTOINCREMENT parity)."""
    db = get_db()
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {
        "id": next_sequence(db, COUNTER_IDS["ingestion_log"]),
        "checked_at": checked,
        "source": result.source,
        "status": status,
        "rows_seen": int(result.rows_seen),
        "inserted": int(result.inserted),
        "updated": int(result.updated),
        "unchanged": int(result.unchanged),
        "latest_observation_date": result.latest_observation_date,
        "message": message,
        "version": SCHEMA_VERSION,
    }
    db.ingestion_log.insert_one(doc)
    return doc


def upsert_frame(frame: pd.DataFrame, source: str = "model_ready_csv") -> SyncResult:
    out = normalize_source_frame(frame)
    inserted = updated = unchanged = 0
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stamps = [row["timestamp"].strftime(DATE_TIME_FORMAT) for _, row in out.iterrows()]
    stamp_set = list(dict.fromkeys(stamps))

    db = get_db()
    ensure_indexes(db)
    existing_rows = {doc["timestamp"]: doc for doc in
                     db.observations.find({"timestamp": {"$in": stamp_set}})}

    operations = []
    for _, row in out.iterrows():
        stamp = row["timestamp"].strftime(DATE_TIME_FORMAT)
        document = {
            "_id": stamp,
            "timestamp": stamp,
            "item_id": str(row["item_id"]),
            **{column: float(row[column]) for column in NUMERIC_COLUMNS},
            "source": source,
            "ingested_at": ingested_at,
            "version": SCHEMA_VERSION,
        }
        existing = existing_rows.get(stamp)
        if existing is None:
            inserted += 1
        else:
            same = (
                document["item_id"] == str(existing.get("item_id", ""))
                and all(abs(float(document[c]) - float(existing.get(c, float("nan")))) < 1e-9
                        for c in NUMERIC_COLUMNS)
            )
            if same:
                unchanged += 1
            else:
                updated += 1
        operations.append(
            ({"_id": stamp},
             {k: v for k, v in document.items() if k != "_id"},
             stamp in existing_rows)
        )

    # Replace semantics: full-row upsert keyed on the date (matches ON CONFLICT DO UPDATE).
    from pymongo.collection import ReplaceOne

    bulk = [
        ReplaceOne(filter, plain, upsert=True)
        for filter, plain, _existing in operations
    ]
    latest = out["timestamp"].max().strftime(DATE_TIME_FORMAT) if not out.empty else None
    result = SyncResult(source, len(out), inserted, updated, unchanged, latest)
    try:
        if bulk:
            db.observations.bulk_write(bulk, ordered=False)
        _log_sync(result)
    except Exception as exc:
        failed = SyncResult(source, len(out), inserted, updated, unchanged,
                            out["timestamp"].max().strftime(DATE_TIME_FORMAT) if not out.empty else None)
        _log_sync(failed, status="error", message=str(exc))
        raise
    return result


def latest_observation_date() -> str | None:
    db = get_db()
    ensure_indexes(db)
    doc = db.observations.find_one({}, {"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)])
    return str(doc["timestamp"]) if doc else None


def existing_dates_between(from_iso: str, to_iso: str) -> set[str]:
    db = get_db()
    rows = db.observations.find(
        {"timestamp": {"$gte": from_iso, "$lte": to_iso}},
        {"_id": 0, "timestamp": 1},
    )
    return {str(r["timestamp"]) for r in rows}


def _validate_dash_updates(series_updates: dict[str, dict[str, float]]) -> None:
    if not isinstance(series_updates, dict):
        raise ValueError("series_updates must be a dict of date -> {column: value}.")
    for stamp, columns in series_updates.items():
        try:
            datetime.strptime(str(stamp), DATE_TIME_FORMAT)
        except ValueError as exc:
            raise ValueError(f"Invalid observation date {stamp!r}; expected YYYY-MM-DD: {exc}") from exc
        unknown = [c for c in columns if c not in NUMERIC_COLUMNS]
        if unknown:
            raise ValueError(f"Unknown observation columns for {stamp}: {unknown}.")
        for column, value in columns.items():
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError(f"Non-finite value for {column} on {stamp}: {value!r}.")


def upsert_dash_graph_observations(
        series_updates: dict[str, dict[str, float]],
        gap_dates: Iterable[str] | None = None,
        update_existing: bool = True) -> dict[str, Any]:
    """Mirror fetched Dash values into graph collections using their canonical units.

    Daily graph rows are completed by carrying forward the preceding row. AP
    monthly arrival totals are refreshed from daily quintals converted to tonnes;
    production and consumption remain owned by the monthly source dataset.
    """
    _validate_dash_updates(series_updates)
    daily_columns = set(DAILY_GRAPH_COLUMNS) - {"timestamp", "item_id"}
    for stamp, columns in series_updates.items():
        unknown = sorted(set(columns) - daily_columns)
        if unknown:
            raise ValueError(f"Columns are not part of daily_price_observations on {stamp}: {unknown}.")

    requested_dates = sorted(set(series_updates) | set(gap_dates or []))
    result: dict[str, Any] = {
        "rows_inserted": 0,
        "values_updated": 0,
        "values_unchanged": 0,
        "gap_rows_filled": 0,
        "monthly_arrivals_updated": 0,
        "monthly_arrival_months_missing": [],
        "latest_observation_date": None,
    }
    if not requested_dates:
        return result

    monthly_arrival_column = QUANTITY_COLUMNS["AP"]["arrival"]
    affected_months: set[str] = set()

    db = get_db()
    ensure_indexes(db)
    daily = db["daily_price_observations"]

    def _get(stamp: str) -> dict | None:
        return daily.find_one({"_id": stamp})

    def _insert(stamp: str, item_id: str, values: dict[str, float]) -> None:
        doc = {"_id": stamp, "timestamp": stamp, "item_id": item_id,
               **{column: float(value) for column, value in values.items()},
               "version": SCHEMA_VERSION}
        daily.insert_one(doc)

    for stamp in requested_dates:
        sourced = series_updates.get(stamp, {})
        if monthly_arrival_column in sourced:
            month = datetime.strptime(stamp, DATE_TIME_FORMAT).replace(day=1)
            affected_months.add(month.strftime(DATE_TIME_FORMAT))

        existing = _get(stamp)
        if existing is not None:
            changes: dict[str, float] = {}
            for column, raw_value in sourced.items():
                value = float(raw_value)
                old_value = float(existing[column])
                if abs(value - old_value) <= TOLERANCE:
                    result["values_unchanged"] += 1
                elif update_existing:
                    changes[column] = value
                    result["values_updated"] += 1
            if changes:
                daily.update_one({"_id": stamp}, {"$set": changes})
            continue

        previous = daily.find_one({"timestamp": {"$lt": stamp}}, sort=[("timestamp", -1)])
        if previous is None:
            raise ValueError(
                "daily_price_observations has no earlier row to carry forward; "
                "load the configured daily dataset before ingestion."
            )

        previous_date = datetime.strptime(str(previous["timestamp"]), DATE_TIME_FORMAT).date()
        target_date = datetime.strptime(stamp, DATE_TIME_FORMAT).date()
        next_date = previous_date + timedelta(days=1)
        while next_date <= target_date:
            day = next_date.strftime(DATE_TIME_FORMAT)
            day_existing = _get(day)
            if day_existing is not None:
                previous = day_existing
            else:
                values = {column: previous[column] for column in DAILY_GRAPH_COLUMNS
                          if column not in ("timestamp", "item_id")}
                day_values = series_updates.get(day, {})
                values.update({column: float(value) for column, value in day_values.items()})
                _insert(day, str(previous["item_id"]), values)
                result["rows_inserted"] += 1
                if day_values:
                    result["values_updated"] += len(day_values)
                else:
                    result["gap_rows_filled"] += 1
                previous = _get(day)
            next_date += timedelta(days=1)

    # AP monthly arrival refresh uses a Mongo aggregation (SUM of daily quintals).
    monthly = db["ap_monthly_observations"]
    for month_start in sorted(affected_months):
        month_date = datetime.strptime(month_start, DATE_TIME_FORMAT)
        next_month = (month_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        monthly_row = monthly.find_one({"_id": month_start})
        if monthly_row is None:
            result["monthly_arrival_months_missing"].append(month_start)
            continue
        agg = list(db["daily_price_observations"].aggregate([
            {"$match": {"timestamp": {"$gte": month_start, "$lt": next_month.strftime(DATE_TIME_FORMAT)}}},
            {"$group": {"_id": None, "total": {"$sum": f"${monthly_arrival_column}"}}},
        ]))
        arrival_quintals = float(agg[0]["total"]) if agg else 0.0
        arrival_tonnes = arrival_quintals / 10.0
        if abs(arrival_tonnes - float(monthly_row["arrival_t_month"])) > TOLERANCE:
            monthly.update_one({"_id": month_start},
                               {"$set": {"arrival_t_month": arrival_tonnes}})
            result["monthly_arrivals_updated"] += 1

    latest_doc = daily.find_one({}, {"timestamp": 1}, sort=[("timestamp", -1)])
    result["latest_observation_date"] = str(latest_doc["timestamp"]) if latest_doc else None
    return result


def classify_dash_updates(series_updates: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Read-only classification of (date, column) values against the current store.

    Returns missing_dates (rows that would be inserted), corrections (existing
    values that differ beyond TOLERANCE; never applied here) and unchanged count.
    Used by pull_upag_dash.py for --dry-run reporting and pre-write attribution.
    """
    _validate_dash_updates(series_updates)
    corrections: list[dict[str, Any]] = []
    unchanged = 0
    missing_dates: list[str] = []
    db = get_db()
    ensure_indexes(db)
    stamps = sorted(series_updates)
    docs = {doc["timestamp"]: doc for doc in
            db.observations.find({"timestamp": {"$in": stamps}})}
    for stamp in stamps:
        row = docs.get(stamp)
        if row is None:
            missing_dates.append(stamp)
            continue
        for column in sorted(series_updates[stamp]):
            new_value = float(series_updates[stamp][column])
            old_value = float(row[column])
            if abs(new_value - old_value) <= TOLERANCE:
                unchanged += 1
            else:
                corrections.append({"date": stamp, "column": column,
                                    "old_value": old_value, "new_value": new_value, "applied": False})
    return {"missing_dates": missing_dates, "corrections": corrections, "unchanged": unchanged}


def upsert_dash_observations(series_updates: dict[str, dict[str, float]],
                             gap_dates: Iterable[str] | None = None,
                             source: str = "upag_dash_6h",
                             source_correction: str = "upag_dash_6h_correction",
                             source_gapfill: str = "upag_dash_6h_gapfill",
                             update_existing: bool = True) -> DashUpsertResult:
    """Partial-aware append/correct upsert for scheduled Dash ingestion.

    - For each date in series_updates with sourced values (ascending order):
      * date absent -> INSERT a complete row: sourced values + every not-sourced
        column forward-filled from the previous row (mirrors the "Interpolated"
        dataset convention). All 24 columns are stored as finite floats.
      * date present -> compare per column: <= TOLERANCE -> unchanged; differs ->
        correction: update ONLY that column (source/source_correction) and record
        before/after; skipped when update_existing is False (logged, not applied).
    - For each date in gap_dates: if still absent, INSERT a full forward-filled
      row (source/source_gapfill) so the daily index never gets a gap.

    The caller is responsible for writing the ingestion_log audit row
    (see record_sync); this function only performs observation-collection writes.
    """
    _validate_dash_updates(series_updates)
    rows_seen = sum(len(columns) for columns in series_updates.values())
    corrections: list[dict[str, Any]] = []
    inserted = updated = unchanged = gap_filled = 0
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    db = get_db()
    ensure_indexes(db)
    observations = db.observations

    current = observations.find_one({}, sort=[("timestamp", -1)])
    if current is None:
        raise ValueError(
            "MongoDB history is empty; import the model-ready CSV first with sync_model_ready_csv.py."
        )
    last_stamp = str(current["timestamp"])
    last_item_id = str(current["item_id"])
    last_values = {c: float(current[c]) for c in NUMERIC_COLUMNS}

    def refresh_last() -> None:
        nonlocal last_stamp, last_values, last_item_id
        row = observations.find_one({}, sort=[("timestamp", -1)])
        if row is not None:
            last_stamp = str(row["timestamp"])
            last_values = {c: float(row[c]) for c in NUMERIC_COLUMNS}
            last_item_id = str(row["item_id"])

    try:
        for stamp in sorted(set(series_updates) | set(gap_dates or [])):
            sourced = series_updates.get(stamp)
            existing = observations.find_one({"_id": stamp})
            if existing is None:
                row_values = {c: last_values[c] for c in NUMERIC_COLUMNS}
                if sourced:
                    row_values.update({c: float(v) for c, v in sourced.items()})
                    inserted += len(sourced)
                    label = source
                else:
                    gap_filled += 1
                    label = source_gapfill
                document = {"_id": stamp, "timestamp": stamp, "item_id": last_item_id,
                            **row_values, "source": label, "ingested_at": ingested_at,
                            "version": SCHEMA_VERSION}
                observations.insert_one(document)
                if stamp > last_stamp:
                    refresh_last()
            elif sourced:
                changes: dict[str, float] = {}
                edits = sorted(sourced)
                for column in edits:
                    new_value = float(sourced[column])
                    old_value = float(existing[column])
                    if abs(new_value - old_value) <= TOLERANCE:
                        unchanged += 1
                    elif update_existing:
                        changes[column] = new_value
                        updated += 1
                        corrections.append({"date": stamp, "column": column, "old_value": old_value,
                                            "new_value": new_value, "applied": True})
                    else:
                        corrections.append({"date": stamp, "column": column, "old_value": old_value,
                                            "new_value": new_value, "applied": False})
                        unchanged += 1
                if changes:
                    set_doc = {**{c: v for c, v in changes.items()},
                               "source": source_correction, "ingested_at": ingested_at}
                    observations.update_one({"_id": stamp}, {"$set": set_doc})
                    if stamp >= last_stamp:
                        refresh_last()
        latest_doc = observations.find_one({}, {"timestamp": 1}, sort=[("timestamp", -1)])
        latest = latest_doc["timestamp"] if latest_doc else None
    except Exception as exc:
        failed = SyncResult(source, rows_seen, inserted, updated, unchanged, None)
        _log_sync(failed, status="error", message=str(exc))
        raise
    return DashUpsertResult(source=source, rows_seen=rows_seen, inserted=inserted, updated=updated,
                            unchanged=unchanged, gap_filled=gap_filled, correction_count=len(corrections),
                            latest_observation_date=str(latest) if latest else None,
                            corrections=corrections)


def record_sync(source: str, status: str, rows_seen: int = 0, inserted: int = 0, updated: int = 0,
                unchanged: int = 0, latest_observation: str | None = None, message: str | None = None) -> None:
    """Public audit-row helper for callers outside this module."""
    _log_sync(SyncResult(source, rows_seen, inserted, updated, unchanged, latest_observation),
              status=status, message=message)


def _mark_source_synced(path: Path) -> None:
    stat = path.stat()
    db = get_db()
    db.source_sync_state.update_one(
        {"source_path": str(path.resolve())},
        {"$set": {"source_path": str(path.resolve()),
                  "mtime_ns": int(stat.st_mtime_ns),
                  "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}},
        upsert=True,
    )


def sync_model_ready_csv(path: str | Path, source: str = "model_ready_csv") -> SyncResult:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")
    frame = pd.read_csv(path, encoding="utf-8-sig")
    result = upsert_frame(frame, source=source)
    if path.resolve() == CSV_PATH.resolve():
        _mark_source_synced(path)
    return result


def ensure_store_ready() -> None:
    db = get_db()
    ensure_indexes(db)
    count = int(db.observations.count_documents({}))
    state = db.source_sync_state.find_one({"source_path": str(CSV_PATH.resolve())})
    if not CSV_PATH.exists():
        if count == 0:
            raise FileNotFoundError(f"No history rows and seed CSV missing at {CSV_PATH}.")
        return
    csv_mtime = CSV_PATH.stat().st_mtime_ns
    if count == 0 or state is None or int(state.get("mtime_ns", -1)) != int(csv_mtime):
        result = sync_model_ready_csv(CSV_PATH, source="initial_csv_seed" if count == 0 else "configured_csv_refresh")
        print(f"Synchronized configured dataset into MongoDB: {result.as_dict()}")


def load_history() -> pd.DataFrame:
    ensure_store_ready()
    db = get_db()
    projection = {column: 1 for column in EXPECTED_COLUMNS} | {"_id": 0}
    docs = list(db.observations.find({}, projection, sort=[("timestamp", 1)]))
    frame = pd.DataFrame(docs, columns=EXPECTED_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    for state in STATE_CODES:
        frame[SUPPLY_COLUMNS[state]] = frame[QUANTITY_COLUMNS[state]["arrival"]] + frame[QUANTITY_COLUMNS[state]["production"]]
    return frame


# ---------------------------------------------------------------------------
# Forecast persistence
# ---------------------------------------------------------------------------

def save_forecast_run(graph_code: str, horizon_days: int, last_observation_date: str, context_hash: str,
                      forecast_values: Iterable[dict[str, Any]], model_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = list(forecast_values)
    db = get_db()
    ensure_indexes(db)
    run_id = next_sequence(db, COUNTER_IDS["forecast_runs"])
    try:
        db.forecast_runs.insert_one({
            "_id": str(run_id),
            "run_id": int(run_id),
            "graph_code": graph_code,
            "generated_at": generated_at,
            "model_id": model_id,
            "horizon_days": int(horizon_days),
            "last_observation_date": last_observation_date,
            "context_hash": context_hash,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, default=str),
            "engine": "mongodb",
            "version": SCHEMA_VERSION,
        })
        if values:
            db.forecast_values.insert_many([
                {"run_id": int(run_id), "state_code": str(v["state_code"]),
                 "target_key": str(v["target_key"]), "forecast_date": str(v["forecast_date"]),
                 "p10": float(v["p10"]), "p50": float(v["p50"]), "p90": float(v["p90"]),
                 "unit": str(v["unit"]), "version": SCHEMA_VERSION}
                for v in values
            ])
    except Exception:
        # Remove a partially-written run so retries never leave orphan docs.
        db.forecast_values.delete_many({"run_id": run_id})
        db.forecast_runs.delete_one({"run_id": int(run_id)})
        raise
    return {"run_id": int(run_id), "generated_at": generated_at, "saved_values": len(values), "graph_code": graph_code}


def get_latest_forecast(graph_code: str | None = None) -> dict[str, Any] | None:
    ensure_store_ready()
    db = get_db()
    match = {"graph_code": graph_code} if graph_code else {}
    row = db.forecast_runs.find_one(match, sort=[("generated_at", -1), ("run_id", -1)])
    if row is None:
        return None
    values = list(db.forecast_values.find(
        {"run_id": int(row["run_id"])},
        {"_id": 0, "run_id": 0},
        sort=[("state_code", 1), ("target_key", 1), ("forecast_date", 1)],
    ))
    result = {key: row[key] for key in row if key not in ("_id", "metadata_json")}
    result["metadata"] = json.loads(row.get("metadata_json") or "{}")
    result["forecast_values"] = values
    return result


def get_latest_run_meta(graph_code: str | None = None) -> dict[str, Any] | None:
    """Latest run header only (no forecast value rows) — cheap freshness check."""
    ensure_store_ready()
    db = get_db()
    match = {"graph_code": graph_code} if graph_code else {}
    row = db.forecast_runs.find_one(
        match,
        {"_id": 0, "metadata_json": 0},
        sort=[("generated_at", -1), ("run_id", -1)],
    )
    return dict(row) if row else None


def list_forecast_runs(limit: int = 20) -> list[dict[str, Any]]:
    ensure_store_ready()
    db = get_db()
    projection = {"_id": 0, "run_id": 1, "graph_code": 1, "generated_at": 1, "model_id": 1,
                  "horizon_days": 1, "last_observation_date": 1, "context_hash": 1}
    cursor = db.forecast_runs.find({}, projection,
                                   sort=[("generated_at", -1), ("run_id", -1)],
                                   limit=max(1, min(int(limit), 100)))
    return list(cursor)


def ingestion_status() -> dict[str, Any]:
    ensure_store_ready()
    db = get_db()
    latest_log = db.ingestion_log.find_one({}, sort=[("id", -1)])
    count = int(db.observations.count_documents({}))
    first_date = db.observations.find_one({}, {"timestamp": 1}, sort=[("timestamp", 1)])
    last_date = db.observations.find_one({}, {"timestamp": 1}, sort=[("timestamp", -1)])
    runs = int(db.forecast_runs.count_documents({}))
    return {"database_path": store_label(),
            "observation_rows": count,
            "first_observation_date": first_date.get("timestamp") if first_date else None,
            "last_observation_date": last_date.get("timestamp") if last_date else None,
            "forecast_run_count": runs,
            "last_ingestion": {k: v for k, v in latest_log.items() if k != "_id"} if latest_log else None,
            "store_engine": "mongodb"}


def ingestion_audit(limit_days: int = 7) -> list[dict[str, Any]]:
    """Aggregation audit: inserted/updated/unchanged sums per source over the
    last N days (MongoDB $group pipeline replacing the SQL audit queries)."""
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(limit_days)))) \
        .isoformat(timespec="seconds")
    pipeline = [
        {"$match": {"checked_at": {"$gte": cutoff}}},
        {"$group": {"_id": "$source",
                    "runs": {"$sum": 1},
                    "total_rows_seen": {"$sum": "$rows_seen"},
                    "total_inserted": {"$sum": "$inserted"},
                    "total_updated": {"$sum": "$updated"},
                    "total_unchanged": {"$sum": "$unchanged"},
                    "last_checked_at": {"$max": "$checked_at"},
                    "statuses": {"$addToSet": "$status"}}},
        {"$project": {"_id": 0, "source": "$_id", "runs": 1, "total_rows_seen": 1,
                      "total_inserted": 1, "total_updated": 1, "total_unchanged": 1,
                      "last_checked_at": 1, "statuses": 1}},
        {"$sort": {"last_checked_at": -1}},
    ]
    return list(db.ingestion_log.aggregate(pipeline))


def supply_balance_snapshot() -> list[dict[str, Any]]:
    """Aggregation pipeline computing AP monthly shortfall =
    consumption - (arrival + production) directly in MongoDB ($addFields)."""
    db = get_db()
    pipeline = [
        {"$addFields": {"shortfall_t_month": {
            "$subtract": ["$consumption_t_month",
                          {"$add": ["$arrival_t_month", "$production_t_month"]}]}}},
        {"$project": {"_id": 0, "month_start": 1, "production_t_month": 1,
                      "consumption_t_month": 1, "arrival_t_month": 1,
                      "shortfall_t_month": 1}},
        {"$sort": {"month_start": 1}},
    ]
    return list(db["ap_monthly_observations"].aggregate(pipeline))


# ---------------------------------------------------------------------------
# Additional aggregation-pipeline helpers (read-only analytics; the store keeps
# wide documents — one date row with per-state columns — so some of the long-form
# pipeline shapes from the migration plan are expressed against the wide schema)
# ---------------------------------------------------------------------------

def latest_series_snapshot(collection: str = "observations",
                           date_field: str = "timestamp") -> list[dict[str, Any]]:
    """Aggregation #1 (latest observation per series): $sort desc -> $group by
    series id -> $first value per series. On this wide schema the 'series'
    key is item_id (one aligned row per date covers every state column)."""
    db = get_db()
    sample = db[collection].find_one({}, {"_id": 0})
    group_id: Any = "$item_id" if (sample and "item_id" in sample) else None
    pipeline = [
        {"$sort": {date_field: -1}},
        {"$group": {"_id": group_id, "latest": {"$first": f"${date_field}"}}},
        {"$project": {"_id": 0, "item_id": "$_id", "latest": 1}},
        {"$sort": {"latest": -1}},
    ]
    return list(db[collection].aggregate(pipeline))


def price_rollups(unit: str = "week",
                  collection: str = "daily_price_observations",
                  columns: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Aggregation #2 (time rollup): avg per calendar week/month over the daily
    graph collection, executed with $dateTrunc when the server supports it
    (>= 5.0; timestamps are ISO strings, so $dateFromString first). Falls back
    to in-Python bucketing on older servers so behavior is version-independent."""
    db = get_db()
    numeric = (list(columns) if columns
               else [c for c in DAILY_GRAPH_COLUMNS if c not in ("item_id", "timestamp")])
    pipeline: list[dict[str, Any]] = [
        {"$addFields": {"_bdate": {"$dateFromString": {"dateString": "$timestamp"}}}},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$_bdate", "unit": unit,
                                   "startOfWeek": "monday"}},
            "count": {"$sum": 1},
            **{column: {"$avg": f"${column}"} for column in numeric},
        }},
        {"$project": {"_id": 0,
                      "bucket": {"$dateToString": {"date": "$_id", "format": DATE_TIME_FORMAT}},
                      "count": 1, **{c: 1 for c in numeric}}},
        {"$sort": {"bucket": 1}},
    ]
    try:
        return list(db[collection].aggregate(pipeline))
    except Exception:
        # Versions without $dateTrunc: mirror the grouping in Python.
        docs = list(db[collection].find({}, {"timestamp": 1, **{c: 1 for c in numeric}, "_id": 0}))
        buckets: dict[str, dict[str, Any]] = {}
        for doc in docs:
            stamp = pd.Timestamp(doc["timestamp"])
            if unit == "week":
                key = (stamp - pd.Timedelta(days=stamp.weekday())).strftime(DATE_TIME_FORMAT)
            else:
                key = stamp.strftime("%Y-%m-01")
            slot = buckets.setdefault(key, {"bucket": key, "count": 0,
                                            **{c: 0.0 for c in numeric}})
            slot["count"] += 1
            for column in numeric:
                slot[column] += float(doc[column])
        out = []
        for key in sorted(buckets):
            slot = buckets[key]
            slot.update({c: slot[c] / slot["count"] for c in numeric if slot["count"]})
            out.append(slot)
        return out


def forecast_with_values(graph_code: str | None = None, limit: int = 1) -> list[dict[str, Any]]:
    """Aggregation #3 (forecast retrieval): latest forecast run(s) with their
    values joined in one server-side $lookup — replaces the former two-step SQL
    read for ad-hoc analytics views (get_latest_forecast keeps identical app
    behaviour with the two-query form)."""
    db = get_db()
    ensure_indexes(db)
    match = {"$match": {"graph_code": graph_code}} if graph_code else {"$match": {}}
    pipeline = [
        match,
        {"$sort": {"generated_at": -1, "run_id": -1}},
        {"$limit": max(1, min(int(limit), 20))},
        {"$lookup": {"from": "forecast_values", "localField": "run_id",
                     "foreignField": "run_id", "as": "values"}},
        {"$project": {"_id": 0}},
        {"$sort": {"generated_at": -1, "run_id": -1}},
    ]
    return list(db.forecast_runs.aggregate(pipeline))


def price_change_snapshot(target_column: str,
                          as_of: str | None = None) -> dict[str, Any] | None:
    """Aggregation #6 (price change): newest observation of `target_column` and
    the one before it, with the delta computed in Python (portable across all
    MongoDB versions)."""
    db = get_db()
    query: dict[str, Any] = {target_column: {"$exists": True, "$ne": None}}
    if as_of:
        query["timestamp"] = {"$lte": as_of}
    docs = list(db.observations.find(query, {"_id": 0},
                                     sort=[("timestamp", -1)], limit=2))
    if not docs:
        return None
    current = docs[0]
    result: dict[str, Any] = {
        "column": target_column,
        "current_date": current["timestamp"],
        "current_value": float(current[target_column]),
    }
    if len(docs) > 1:
        previous = docs[1]
        previous_value = float(previous[target_column])
        change = result["current_value"] - previous_value
        result.update({
            "previous_date": previous["timestamp"],
            "previous_value": previous_value,
            "change": change,
            "change_percent": (change / previous_value * 100.0) if previous_value else None,
        })
    return result
