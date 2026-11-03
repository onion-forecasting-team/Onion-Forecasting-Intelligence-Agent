"""UPAg Open API discovery / raw source fetch utility.

Credentials are read only from the local .env file. Never paste passwords or access
 tokens into source code, chat, or git. This tool intentionally does not transform an
 unknown source schema into model-ready rows; confirm the selected source's payload and
 response fields first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / "config" / ".env")
DEFAULT_BASE = os.getenv("UPAG_BASE_URL", "https://data.upag.gov.in").rstrip("/")
LOGIN_PATH = "/v1/upag/api-data-share/login"
SOURCES_PATH = "/v1/upag/api-data-share/sources/user-allowed-sources"
ALLOWED_SOURCE_NAMES = {
    "dafw_state", "mncfc", "ieg", "dcs", "fci_procurement", "fci_stock", "dgcis",
    "dafw_district", "farmers_survey", "nsso", "cwwg", "pmfby_ay", "trs",
    "state_reported", "agmarknet", "enam", "doca", "horticulture", "sopa", "wpi",
    "ncdex", "pink_sheet",
}


class UPAgClient:
    def __init__(self) -> None:
        self.base_url = DEFAULT_BASE
        self.username = os.getenv("UPAG_USERNAME", "").strip()
        self.password = os.getenv("UPAG_PASSWORD", "")
        self.client_id = os.getenv("UPAG_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("UPAG_CLIENT_SECRET", "")
        self.scope = os.getenv("UPAG_SCOPE", "")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "onion-forecasting-mvp/0.1"})
        self._token: str | None = None

    def login(self) -> str:
        if not self.username or not self.password:
            raise RuntimeError(
                "UPAG_USERNAME and UPAG_PASSWORD are missing in .env. Put credentials in the local .env file; do not paste them into chat or code."
            )
        form = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
            "scope": self.scope,
        }
        if self.client_id:
            form["client_id"] = self.client_id
        if self.client_secret:
            form["client_secret"] = self.client_secret
        response = self.session.post(self.base_url + LOGIN_PATH, data=form, timeout=30)
        if not response.ok:
            raise RuntimeError(
                f"UPAg login failed with HTTP {response.status_code}. Response: {response.text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("UPAg login response was not JSON.") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            safe_keys = list(payload.keys()) if isinstance(payload, dict) else []
            raise RuntimeError(f"Login response did not contain access_token. Response keys: {safe_keys}")
        self._token = str(token)
        self.session.headers["Authorization"] = f"Bearer {self._token}"
        return self._token

    def allowed_sources(self) -> Any:
        if not self._token:
            self.login()
        response = self.session.get(self.base_url + SOURCES_PATH, timeout=30)
        if not response.ok:
            raise RuntimeError(
                f"Could not fetch allowed UPAg sources (HTTP {response.status_code}): {response.text[:500]}"
            )
        return response.json()

    def fetch_source(self, source_name: str, source_input_object: dict[str, Any]) -> Any:
        if source_name not in ALLOWED_SOURCE_NAMES:
            raise ValueError(f"Unsupported UPAg source name '{source_name}'.")
        if not self._token:
            self.login()
        url = f"{self.base_url}/v1/upag/api-data-share/sources/{source_name}"
        response = self.session.post(
            url,
            json={"source_input_object": source_input_object},
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(
                f"UPAg source request failed with HTTP {response.status_code}: {response.text[:1000]}"
            )
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-sources", action="store_true", help="Authenticate and list source names allowed for this account")
    parser.add_argument("--source", choices=sorted(ALLOWED_SOURCE_NAMES), help="Fetch a single allowed source")
    parser.add_argument("--payload-file", help="JSON file containing that source's source_input_object")
    parser.add_argument("--output", help="Where to save raw source JSON; defaults to data/upag_raw_<source>.json")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="Override UPAg API base URL")
    args = parser.parse_args()
    if not args.list_sources and not args.source:
        parser.error("Choose --list-sources first, or provide --source and --payload-file.")
    try:
        client = UPAgClient()
        client.base_url = args.base_url.rstrip("/")
        if args.list_sources:
            payload = client.allowed_sources()
            # This object is not a token. Print source permissions only.
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            if not args.source:
                return 0
        if args.source:
            if not args.payload_file:
                parser.error("--source requires --payload-file based on the official documentation for that source.")
            payload_path = Path(args.payload_file)
            if not payload_path.is_absolute():
                payload_path = (ROOT / payload_path).resolve()
            source_input = json.loads(payload_path.read_text(encoding="utf-8"))
            if "source_input_object" in source_input and isinstance(source_input["source_input_object"], dict):
                source_input = source_input["source_input_object"]
            result = client.fetch_source(args.source, source_input)
            output_path = Path(args.output) if args.output else ROOT / "data" / f"upag_raw_{args.source}.json"
            if not output_path.is_absolute():
                output_path = (ROOT / output_path).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            summary = {"saved_to": str(output_path), "response_type": type(result).__name__}
            if isinstance(result, dict):
                summary["top_level_keys"] = list(result.keys())
                data = result.get("data")
                if isinstance(data, list):
                    summary["data_row_count"] = len(data)
                    summary["first_row_keys"] = list(data[0].keys()) if data and isinstance(data[0], dict) else []
            print(json.dumps(summary, indent=2))
        return 0
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"UPAG ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
