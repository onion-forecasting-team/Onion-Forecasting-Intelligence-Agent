"""Chat session/message persistence in the shared MongoDB store.

Replaces the former SQLite chat tables with two MongoDB collections that keep
the SAME document fields, so the chatbot's contract is unchanged:
  chat_sessions : {session_id, created_at, updated_at, user_label, title}
  chat_messages : {seq, session_id, role, content, tools_used(json str), chart_kind, created_at}

`list_sessions` counts messages live via a $lookup aggregation (replaces the
SQL correlated subquery) and never needs idempotent DDL — MongoDB is
schemaless. Chart payloads are NOT persisted — replaying a chart means a
fresh API call; forecast numbers themselves live in forecast_runs/
forecast_values already.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from db_client import COUNTER_IDS, SCHEMA_VERSION, ensure_indexes, get_db, next_sequence

DEFAULT_TITLE = "New chat"
_TITLE_LIMIT = 48


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_chat_schema() -> None:
    """Idempotent index setup (replaces CREATE TABLE IF NOT EXISTS)."""
    ensure_indexes(get_db())


def create_session(user_label: str | None = None, title: str = DEFAULT_TITLE) -> dict[str, Any]:
    db = get_db()
    session_doc = {
        "session_id": uuid.uuid4().hex,
        "created_at": _now(),
        "updated_at": _now(),
        "user_label": user_label,
        "title": title,
        "messages_count": 0,
        "version": SCHEMA_VERSION,
    }
    db.chat_sessions.insert_one(session_doc)
    return {key: session_doc[key] for key in
            ("session_id", "created_at", "updated_at", "user_label", "title")}


def get_session(session_id: str) -> dict[str, Any] | None:
    doc = get_db().chat_sessions.find_one(
        {"session_id": session_id},
        {"_id": 0, "messages_count": 0, "version": 0},
    )
    return doc


def touch_session(session_id: str) -> None:
    get_db().chat_sessions.update_one(
        {"session_id": session_id}, {"$set": {"updated_at": _now()}})


def set_session_title(session_id: str, title: str) -> None:
    get_db().chat_sessions.update_one(
        {"session_id": session_id},
        {"$set": {"title": title.strip()[:_TITLE_LIMIT] or DEFAULT_TITLE,
                  "updated_at": _now()}})


def auto_title_if_default(session_id: str, first_message: str) -> bool:
    """Set the title from the first user message unless the user already sees one."""
    session = get_session(session_id)
    if session and session["title"] == DEFAULT_TITLE:
        set_session_title(session_id, first_message.strip() or DEFAULT_TITLE)
        return True
    return False


def append_message(session_id: str, role: str, content: str,
                   tools_used: list[str] | None = None, chart_kind: str | None = None) -> None:
    db = get_db()
    ensure_indexes(db)
    db.chat_messages.insert_one({
        "seq": next_sequence(db, COUNTER_IDS["chat_messages"]),
        "session_id": session_id,
        "role": role,
        "content": content,
        "tools_used": json.dumps(tools_used or [], ensure_ascii=False),
        "chart_kind": chart_kind,
        "created_at": _now(),
        "version": SCHEMA_VERSION,
    })
    touch_session(session_id)


def get_history(session_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Newest-last conversation slice (role/content only for the LLM)."""
    ensure_chat_schema()
    safe_limit = max(1, min(int(limit), 200))
    docs = list(get_db().chat_messages.find(
        {"session_id": session_id},
        {"_id": 0, "version": 0},  # `version` is bookkeeping; never leaks to API responses
        sort=[("seq", -1)],
        limit=safe_limit,
    ))
    # Keep the former row shape with integer `id` (SQLite parity).
    return [{**doc, "id": doc["seq"]} for doc in reversed(docs)]


def message_count(session_id: str) -> int:
    return int(get_db().chat_messages.count_documents({"session_id": session_id}))


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    ensure_chat_schema()
    safe_limit = max(1, min(int(limit), 200))
    pipeline = [
        {"$lookup": {
            "from": "chat_messages",
            "localField": "session_id",
            "foreignField": "session_id",
            "as": "messages",
        }},
        {"$project": {
            "_id": 0,
            "session_id": 1,
            "title": 1,
            "created_at": 1,
            "updated_at": 1,
            "user_label": 1,
            "messages_count": {"$size": "$messages"},
        }},
        {"$sort": {"updated_at": -1, "created_at": -1}},
        {"$limit": safe_limit},
    ]
    return list(get_db().chat_sessions.aggregate(pipeline))
