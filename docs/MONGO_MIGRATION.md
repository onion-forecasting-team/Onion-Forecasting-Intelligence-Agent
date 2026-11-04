# SQLite → MongoDB Migration — End-to-End Documentation

**Project:** Onion Forecasting Intelligence Agent (Chronos-2 MVP)
**Scope:** Full storage-engine conversion of the data layer from a local SQLite file
(`data/onion_forecasting_v4.sqlite3`) to a managed MongoDB store (Atlas, database
`onion_forecasting`) with **zero behavior change** for the API, dashboard, chatbot,
or ingestion pipeline.
**Status:** Code conversion complete and verified (`verify_migration.py → all_ok: true`,
70/70 offline tests passing against Atlas).

---

## 1. Why we converted

| Constraint with SQLite | What MongoDB gives us here |
|---|---|
| Single local file on one PC — ingestion, the API server, and the dashboard all had to sit on the same machine and same file handle | Managed Atlas cluster reachable from any host that runs the components; replication built-in |
| Row locking (WAL) could bottleneck concurrent API reads during 6-hourly ingestion writes | Document-level concurrency; reads never block the ingestor |
| Schema evolution means `ALTER TABLE` migrations on a 2 GB working file | Flexible documents; additive fields (`version: 1`) track schema without destructive migrations |
| No native aggregation language — analytics meant hand-written SQL queries in Python strings | Full aggregation pipelines ($lookup, $group, $addFields, $dateTrunc) executed server-side |
| Weak tooling for atomic sequence counters, upsert semantics at scale, and TTL/audit retention | `$inc` counters, `ReplaceOne` upserts, TTL options, profiler and cloud backups |
| Backups = raw file copies | Atlas continuous backups + `mongodump` + the retained SQLite backup as a third tier |

Non-goals (deliberately **not** part of the migration): no changes to forecasting
logic, validation rules, unit semantics, API contracts, or the CSV dataset workflow.
The SQL logic in `history_store.py` was translated, not redesigned.

---

## 2. Architecture before / after

```
BEFORE                                AFTER
─────────────────────────────────     ─────────────────────────────────────
SQLite (WAL file, one process-ish)    MongoDB Atlas (replica set atlas-k4xkz2-shard-0)
data/onion_forecasting_v4.sqlite3     database: onion_forecasting

tables (~10)                          collections (mirrored 1:1)
  observations                  →     observations            _id = timestamp
  daily_price_observations      →     daily_price_observations _id = timestamp
  ap_monthly_observations       →     ap_monthly_observations  _id = month_start
  forecast_runs                 →     forecast_runs            _id = str(run_id)
  forecast_values               →     forecast_values          (auto ObjectId)
  ingestion_log                 →     ingestion_log            + sequential `id`
  source_sync_state             →     source_sync_state        (keyed by source_path)
  graph_dataset_sync_state      →     graph_dataset_sync_state (keyed by dataset_key)
  chat_sessions                 →     chat_sessions            (unique session_id)
  chat_messages                 →     chat_messages            + sequential `seq`
  (AUTOINCREMENT internal)      →     counters                 (apps-managed)
```

Key rule: **documents mirror the former table rows field-for-field.** Column names
(`AP_retail_rs_per_metric_quintal`, …), ISO date strings, units, and tolerance
thresholds are untouched, so every pandas conversion and API response is identical.

Document `_id` reuses the former SQL primary keys (natural keys). This is what makes
upserts idempotent: writing the same date again replaces the same document.

---

## 3. What was done, step by step (mapped to the migration plan)

### Phase 1 — Security & environment
- `config/.env`: added `MONGO_URI` + `MONGO_DB_NAME` (kept `ONION_DB_PATH` only as the
  SQLite **backup** path for the verifier).
- `requirements.txt`: `pymongo>=4.10.0` installed into `.venv`.
- Atlas Network Access whitelist for this machine's IP (was required — non-whitelisted
  IPs are rejected at the TLS layer, which initially surfaced as
  `TLSV1_ALERT_INTERNAL_ERROR` handshake failures).
- The connection password was exposed in chat during planning and **must be rotated**
  in Atlas; only the rotated value belongs in `MONGO_URI`.

### Phase 2 — Data movement & validation
- Initial dump: SQLite tables → MongoDB collections via the user's own dump
  (to their belief complete).
- Validation revealed three dump defects, which became explicit ops fixes:
  1. Dumped documents carried default **ObjectId `_id`s** instead of natural keys →
     the app's `find_one({"_id": stamp})` upsert reads would miss migrated rows.
  2. The dump missed a forecast run + its values written after the snapshot
     (SQLite: 38 runs / 1228 values vs MongoDB: 37 / 1208).
  3. `sqlite_sequence` (SQLite's internal counter table) was dumped as an inert
     collection.
- Fix chain (all idempotent, committed as scripts):
  `rebuild_natural_ids.py` → natural-key `_id`s for the four keyed collections;
  `import_missing_forecasts.py` → import missing run/value rows from the SQLite backup;
  `bootstrap_counters.py` → drop `sqlite_sequence`, shim `chat_messages.id → seq`.
- When the rebuild hit a mid-run interruption, the definitive path used was the
  **canonical restore**: `restore_from_sqlite_backup.py --yes` rebuilt every mapped
  collection directly from the retained SQLite backup with correct `_id`s,
  `version: 1` stamps, and re-seeded counters. Result back to exact parity
  (1767 / 1767 / 58 / 38 / 1228 / 6 / 13 / 63).

### Phase 3 — Index strategy
Created once per database by `db_client.ensure_indexes()` (idempotent, logged, never
crashes startup):

| Collection | Indexes |
|---|---|
| `observations` | `{timestamp: 1} unique` (the PK), `{timestamp, item_id}` |
| `daily_price_observations` | `{timestamp: 1} unique` |
| `ap_monthly_observations` | `{month_start: 1} unique` |
| `ingestion_log` | `{id: -1}`, `{source: 1}`, `{status: 1}`, `{checked_at: -1}` |
| `forecast_runs` | `{graph_code: 1}`, `{generated_at: -1}`, `{run_id: -1}`, `{graph_code, generated_at}` |
| `forecast_values` | four single-field + `{run_id, forecast_date}` compound |
| `chat_sessions` | `{session_id: 1} unique`, `{updated_at: -1}` |
| `chat_messages` | `{session_id: 1}`, `{seq: -1}`, `{session_id, seq}` |

Note on the plan's `{AP_state, price_type}` idea: those fields don't exist — the store
is **wide** (one document per date, one field per state column), so the natural
compound keys are the ones above.

### Phase 4 — Data-layer rewrite (`src/data_layer/history_store.py`)
Everything below preserved the former function signatures exactly; callers
(`main.py`, `pull_upag_dash.py`, `chatbot/tools.py`, `dash_extract.py`,
`sync_model_ready_csv.py`) did not have to change their logic:

| former SQLite technique | MongoDB equivalent (implementation) |
|---|---|
| `sqlite3.connect()` | single pooled `MongoClient` per process (`db_client.get_client()`, `maxPoolSize=50`, server/socket timeouts, `appname`) |
| `PRIMARY KEY` columns | document `_id` = natural key (date / month) + unique indexes |
| `INSERT … ON CONFLICT DO UPDATE` (row upsert) | `ReplaceOne(filter, doc, upsert=True)`; bulk path uses one `{timestamp: $in}` read, in-memory inserted/updated/unchanged classification, then one `bulk_write` |
| partial row updates (`UPDATE … SET col=…`) | `update_one({"_id": stamp}, {"$set": …})` — only corrected columns change, `source: …_correction` and `ingested_at` preserved |
| `AUTOINCREMENT` ids | `counters` collection + atomic `find_one_and_update($inc)` (`next_sequence()`); run ids, audit ids, chat seqs are monotonic and never reused |
| `pd.read_sql` loads | `find()` with projections (no `_id` leak), `sort` on the date field |
| mtime-tracked CSV reseed guard | `graph_dataset_sync_state` / `source_sync_state` upserted state docs (same field names) |
| DID-EXIST probes during ingestion | `find_one({"_id": stamp})` — O(1) index hit |

Ingestion semantics kept 1:1 from the SQLite era: `TOLERANCE = 1e-6` for identical-value
checks, per-column corrections with before/after records, forward-fill from the previous
row for unsourced columns, gap-fill rows labeled `_gapfill`, and `refresh_last` bookkeeping.
Validation runs **before** any write: `normalize_source_frame`, `_validate_graph_frame`,
duplicate-date/missing-value/non-finite checks.

### Phase 5 — Chatbot & API
- `src/chatbot/sessions.py`: same public functions; `chat_messages.seq` = counter id
  (SQLite `id` parity, and a shim sets `seq = id` for migrated docs);
  `list_sessions` counts messages live via a `$lookup`.
- `src/api/main.py`: `/health` reports `history_db` as a **credential-redacted** Atlas
  store label (`mongodb+srv://***@cluster.../onion_forecasting`) plus
  `store_engine: "mongodb"`. The response key `saved_to_sqlite` is deliberately kept
  (historic API contract) even though it now means "saved to the database".
- `chatbot/agent.py`, `chatbot/tools.py`, `src/ui/dashboard.py`: **zero changes**.

### Phase 6 — Aggregation pipelines (7 types, all server-side)
Implemented in `history_store.py` / `sessions.py`:

1. **Latest per series** — `latest_series_snapshot()`: `$sort → $group → $first`
   (wide schema: series = `item_id`).
2. **Time rollup** — `price_rollups(unit="week"|"month")`:
   `$dateFromString → $dateTrunc → $avg`, **with a pure-Python fallback** for
   MongoDB < 5.0 so results are version-independent.
3. **Forecast retrieval join** — `forecast_with_values(graph_code)`:
   `$match → $sort → $limit → $lookup(forecast_values)` — replaces the old two-step
   read (the app still uses the two-query `get_latest_forecast` for contract parity).
4. **Supply balance** — `supply_balance_snapshot()`:
   `$addFields` computes `shortfall_t_month = consumption − (arrival + production)`.
5. **Ingestion audit** — `ingestion_audit(days)`:
   `$match(checked_at >= cutoff) → $group(by source) → $sum/$max/$addToSet`.
6. **Price change** — `price_change_snapshot(column, as_of)`:
   newest + previous docs fetched, delta computed in Python (portable everywhere).
7. **Chat metrics** — `message_count(session_id)` (`count_documents`) plus the
   `$lookup`-based `list_sessions`.
And inline in the ingest path: the monthly AP arrival refresh runs a
`$group SUM` over daily quintals then converts to tonnes.

### Phase 7 — Testing
- Root `conftest.py` added (test import paths were broken on this machine before the
  conversion work).
- All three suites ported to a sealed **`onion_forecasting_test`** database:
  fixture purges/drops it per test; `MONGO_DB_NAME` is monkeypatched, never the real DB.
- Safety guard: `drop/purge_database_collections()` refuses any database whose name
  doesn't contain `_test`/`sandbox` — production cannot be wiped from a test run.
- Final state: **70/70 tests pass** (the CORS preflight test was made
  `CORS_ORIGINS`-config aware, and the fallback-model test derives model names from
  `agent._cfg()` instead of hardcoding the old author's Groq config).

### Phase 8 — Backup / monitoring / docs
- `scripts/restore_from_sqlite_backup.py --yes` — canonical offline restore (proof it
  works: this script IS what fixed the store).
- `docs/COMMANDS.md §4` — `mongodump/mongorestore` commands + Atlas continuous backup
  guidance + slow-query profiler one-liner.
- `README.md`, `API.md`, `CHATBOT_INTEGRATION.md`, `COMMANDS.md` all converted to
  MongoDB wording; this document added.
- SQLite retained for **one week of stable operation**, then archive.

---

## 4. Verification methodology

`scripts/verify_migration.py` compares the SQLite backup against the live store:

1. Row count vs document count per table/collection.
2. Full primary-key coverage (every SQLite key present in MongoDB).
3. Numeric + textual spot-check of the newest observation row across all matched
   columns (28 columns on the current data) — and it **fails loudly** if the `_id`
   layout is wrong instead of passing vacuously.

Final result of the production store — `all_ok: true`:

```
observations 1767=1767 | daily_price_observations 1767=1767
ap_monthly_observations 58=58 | ingestion_log 6=6
forecast_runs 38=38 | forecast_values 1228=1228
chat_sessions 13=13 | chat_messages 63=63
sync-state tables 1=1 / 2=2      spot_check_columns 28, faults 0
counters: forecast_run_id=38, ingestion_log_id=7, chat_message_seq=63
```

`version: 1` was backfilled onto every pre-migration document by
`init_mongodb_indexes.py` and stamped on every new write.

---

## 5. Ops scripts inventory

| Script | Purpose | Idempotent |
|---|---|---|
| `scripts/init_mongodb_indexes.py` | ping + create all indexes + counters bootstrap + `version` backfill + alias-name detection + hint | yes |
| `scripts/bootstrap_counters.py` | seed/raise `counters` to existing maxima; shim `chat_messages.id→seq`; drop `sqlite_sequence` | yes |
| `scripts/rebuild_natural_ids.py` | replace ObjectId `_id`s with natural keys (date/month/run_id), dedupe | yes |
| `scripts/import_missing_forecasts.py` | import forecast runs/values missing from the dump | yes (skips existing run_ids) |
| `scripts/restore_from_sqlite_backup.py --yes` | canonical restore of all mapped collections from the SQLite backup | destructive, guarded |
| `scripts/verify_migration.py` | parity report (above) | read-only |
| `scripts/align_collection_names.py` | rename plan-alias collections (`daily_prices`, `monthly_balances`, `ingestion_logs`) → canonical names | yes (was not needed here) |
| `scripts/export_observations_csv.py` | regenerate a missing seed CSV from the `observations` table | overwrites target CSV |

Runbook order for bring-up / fresh machine:
`init_mongodb_indexes.py` → `align_collection_names.py` (only if aliases detected)
→ `verify_migration.py` → `pytest -q` → `backend.py`.

---

## 6. What deliberately did NOT change

- Every existing endpoint's request/response shapes (including the legacy
  `saved_to_sqlite` key), status codes, and error messages.
- Chart/dashboard behavior (`dashboard.py` untouched).
- Units: ₹/metric quintal stored, ₹/kg returned, tonnes/month for monthly data.
- Timestamp semantics: calendar dates remain `YYYY-MM-DD` strings (Asia/Kolkata
  days exactly as captured); audit `*_at` fields remain UTC ISO-8601.
- Files: `pull_upag_dash.lock`, `corrections_log.csv`, `data/snapshots/` (not DB-dependent).
- The CSV workflow (model-ready CSV, daily price CSV, monthly CSV) and their
  mtime-tracked sync guards.

## 7. Security notes

- The original connection password was pasted in chat multiple times → rotate in
  Atlas Database Access; `MONGO_URI` in `config/.env` must hold only the rotated value.
- Atlas Network Access: whitelist the operator IP(s). Un-whitelisted connections fail
  at the TLS handshake, not at auth — that's the diagnostic signature.
- `/health` and audit outputs never print credentials (`store_label()` redacts).
- Live app is local MVP: CORS defaults to `*` (set `CORS_ORIGINS` to explicit origins
  before ever exposing the API publicly). All mutating endpoints stay behind the
  Bearer/ingestion keys, unchanged.

## 8. Rollback path

1. Stop the backend; the SQLite file is untouched since the switch.
2. Optionally replay the final week of ingestion into SQLite with legacy tooling —
   or accept that MongoDB is the forward store.
3. `restore_from_sqlite_backup.py --yes` can rebuild MongoDB at any time (it is the
   proven recovery path for a corrupted/damaged store).
4. Keep `data/onion_forecasting_v4.sqlite3` + `.bak` one week after go-live, then
   archive both (logs of drift make them stale).

## 9. Source map (files touched by the conversion)

```
created:  src/data_layer/db_client.py            (connection/index/counter layer)
rewritten: src/data_layer/history_store.py       (MongoDB backend, same public API)
rewritten: src/chatbot/sessions.py               (chat collections)
edited:   src/api/main.py                        (imports, /health, CORS default, docs string)
edited:   config/.env +.env.example, requirements.txt, README/API/CHATBOT/COMMANDS docs
created:  conftest.py, tests rewritten to onion_forecasting_test, scripts/* (§5)
untouched: pull_upag_dash.py's ingestion logic shape, upag/dash clients, dashboard.py,
           agent.py, tools.py, prompts, chronos/graph endpoint logic
```
