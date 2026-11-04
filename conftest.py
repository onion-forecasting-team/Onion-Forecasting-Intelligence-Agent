"""Pytest path bootstrap: exposes the packages/modules the tests import.

- `src/data_layer` : history_store, db_client, dash_client, dash_extract, pull_upag_dash
- `src/chatbot`    : the chatbot package (agent, sessions, tools, chat_router)
- `src/api`        : main (FastAPI app)
- `src`            : for `from chatbot.chat_router import ...`
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for relative in ("src", "src/data_layer", "src/chatbot", "src/ui", "src/api"):
    site = str(ROOT / relative)
    if site not in sys.path:
        sys.path.insert(0, site)
