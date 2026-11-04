"""MongoDB connection + shared store utilities for the onion forecasting codebase.

Replaces the original SQLite connection layer. Every collection mirrors the
former SQLite table 1:1 (same document field names), so the pandas/analysis
code paths behave identically.

Environment (config/.env):
  MONGO_URI          connection string, e.g. mongodb+srv://user:<password>@cluster.../ ong
  MONGO_DB_NAME      database name (default: onion_forecasting)

Production notes:
- ONE pooled MongoClient is created per process (maxPoolSize=50).
- Sequential integer ids (SQLite AUTOINCREMENT parity) come from a `counters`
  collection using an atomic find_one_and_update $inc.
- Indexes are created once per database via init_indexes() (idempotent).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / "config" / ".env")

_logger = logging.getLogger("onion-db-client")

DEFAULT_DB_NAME = "onion_forecasting"
# Bumped whenever a document shape gains new fields; stamped on every write and
# backfilled onto pre-migration documents by scripts/init_mongodb_indexes.py.
SCHEMA_VERSION = 1
_CLIENT_LOCK = threading.Lock()
_CLIENT_CACHE: dict[str, object] = {}
_INDEXED_DATABASES: set[str] = set()
_INDEX_LOCK = threading.Lock()

# Indexes created per database (collection -> list of (keys, options)).
# `keys` is a list of (field, direction) pairs — a single pair for simple
# indexes, several pairs for compound indexes. `options` are create_index kwargs.
# NOTE on the "wide" schema: observations/daily_price_observations store one
# document per date with one field per state column (e.g. AP_retail_...), so the
# natural compound keys are (timestamp, item_id) rather than a long-form
# (state, price_type) layout.
_INDEX_PLAN: dict[str, list[tuple[list, dict]]] = {
    "observations": [
        ([("timestamp", 1)], {"unique": True}),
        ([("timestamp", 1), ("item_id", 1)], {}),
    ],
    "daily_price_observations": [
        ([("timestamp", 1)], {"unique": True}),
    ],
    "ap_monthly_observations": [
        ([("month_start", 1)], {"unique": True}),
    ],
    "source_sync_state": [
        ([("source_path", 1)], {"unique": True}),
    ],
    "graph_dataset_sync_state": [
        ([("dataset_key", 1)], {"unique": True}),
    ],
    "ingestion_log": [
        ([("id", -1)], {}),
        ([("source", 1)], {}),
        ([("status", 1)], {}),
        ([("checked_at", -1)], {}),
    ],
    "forecast_runs": [
        ([("graph_code", 1)], {}),
        ([("generated_at", -1)], {}),
        ([("run_id", -1)], {}),
        ([("graph_code", 1), ("generated_at", -1)], {}),
    ],
    "forecast_values": [
        ([("run_id", 1)], {}),
        ([("state_code", 1)], {}),
        ([("target_key", 1)], {}),
        ([("forecast_date", 1)], {}),
        ([("run_id", 1), ("forecast_date", 1)], {}),
    ],
    "chat_sessions": [
        ([("session_id", 1)], {"unique": True}),
        ([("updated_at", -1)], {}),
    ],
    "chat_messages": [
        ([("session_id", 1)], {}),
        ([("seq", -1)], {}),
        ([("session_id", 1), ("seq", -1)], {}),
    ],
}

COUNTER_IDS = {
    "forecast_runs": "forecast_run_id",
    "ingestion_log": "ingestion_log_id",
    "chat_messages": "chat_message_seq",
}


class MongoDBConfigError(RuntimeError):
    """Raised when MONGO_URI is missing/unreachable-critical configuration."""


def _require_uri() -> str:
    uri = os.getenv("MONGO_URI", "").strip().strip("'\"")
    if not uri:
        raise MongoDBConfigError(
            "MONGO_URI is not configured in config/.env. "
            "Set it to the Atlas connection string, e.g. "
            "MONGO_URI=mongodb+srv://<user>:<password>@<cluster-host>/")
    return uri


def get_client(uri: str | None = None):
    """Return the process-wide pooled MongoClient (one per URI)."""
    target_uri = uri or _require_uri()
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(target_uri)
        if client is None:
            # pymongo is imported lazily so the module can be imported for
            # constants (column maps) without the driver installed.
            try:
                from pymongo import MongoClient
            except ImportError as exc:  # pragma: no cover - environment error
                raise MongoDBConfigError(
                    "The 'pymongo' package is missing. Activate the project "
                    "virtual environment and run: pip install -r requirements.txt") from exc
            client = MongoClient(
                target_uri,
                serverSelectionTimeoutMS=8000,
                connectTimeoutMS=8000,
                socketTimeoutMS=20000,
                maxPoolSize=50,
                appname="onion-forecasting-agent",
            )
            _CLIENT_CACHE[target_uri] = client
        return client


def database_name(override: str | None = None) -> str:
    if override:
        return override
    return os.getenv("MONGO_DB_NAME", DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME


def get_db(name: str | None = None):
    """Return the working Database object. Name resolves per call so tests can
    point to a throwaway database via the MONGO_DB_NAME environment variable."""
    return get_client()[database_name(name)]


def _redact(uri: str) -> str:
    """Strip credentials from a Mongo URI before it appears in logs/responses."""
    try:
        parsed = urlparse(uri)
        if "@" not in (parsed.netloc or ""):
            return uri
        host = parsed.netloc.split("@", 1)[1]
        shown = parsed._replace(netloc=f"***@{host}")
        return urlunparse(shown)
    except Exception:
        return "mongodb://***redacted***"


def store_label(name: str | None = None) -> str:
    """Human-readable, credential-free store identity for /health responses."""
    return f"{_redact(_require_uri()).rstrip('/')}/{database_name(name)}"


def ping(name: str | None = None) -> dict:
    """Verify connectivity; returns server/basic info for diagnostics."""
    db = get_db(name)
    result = db.command("ping")
    return {"ok": result.get("ok"), "database": database_name(name),
            "store": store_label(name)}


def next_sequence(db, counter_name: str) -> int:
    """Atomic monotonic integer, mirroring SQLite AUTOINCREMENT behaviour."""
    from pymongo import ReturnDocument
    result = db.counters.find_one_and_update(
        {"_id": counter_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(result["seq"])


def ensure_indexes(db) -> None:
    """Idempotent index creation; runs once per (process, database name)."""
    name = db.name
    with _INDEX_LOCK:
        if name in _INDEXED_DATABASES:
            return
        for collection, specs in _INDEX_PLAN.items():
            target = db[collection]
            for keys, options in specs:
                try:
                    target.create_index(list(keys), **options)
                except Exception as exc:
                    # Non-fatal (e.g. an equivalent index already exists under a
                    # different name from the initial dump), but never hidden.
                    _logger.warning("create_index failed on %s.%s %s: %s",
                                    db.name, collection, list(keys), exc)
        _INDEXED_DATABASES.add(name)


def reset_index_flag(db_name: str | None = None) -> None:
    """Forget the 'already indexed' marker (used by tests after dropping collections)."""
    with _INDEX_LOCK:
        _INDEXED_DATABASES.discard(database_name(db_name))


def drop_database_collections(db_name: str | None = None) -> dict:
    """Drop every collection in a database. Guarded to test databases only so a
    production store can never be accidental: the name must contain '_test' or
    sandbox."""
    name = database_name(db_name)
    lowered = name.lower()
    if "_test" not in lowered and "sandbox" not in lowered:
        raise MongoDBConfigError(
            f"Refusing to drop database '{name}': drop_database_collections is "
            "restricted to *_test* / *sandbox* databases.")
    db = get_db(name)
    dropped = []
    for collection in db.list_collection_names():
        db[collection].drop()
        dropped.append(collection)
    reset_index_flag(name)
    return {"database": name, "dropped": dropped}


def purge_database_collections(name: str | None = None) -> dict:
    """Same guard as drop_database_collections, implemented with delete_many.
    Kept for test fixtures that want counters/counters caches reset quickly."""
    db_name = database_name(name)
    lowered = db_name.lower()
    if "_test" not in lowered and "sandbox" not in lowered:
        raise MongoDBConfigError(
            f"Refusing to purge database '{db_name}': purge is restricted to "
            "*_test* / *sandbox* databases.")
    db = get_db(db_name)
    purged: dict[str, int] = {}
    for collection in db.list_collection_names():
        purged[collection] = db[collection].delete_many({}).deleted_count
    reset_index_flag(db_name)
    return {"database": db_name, "purged": purged}
