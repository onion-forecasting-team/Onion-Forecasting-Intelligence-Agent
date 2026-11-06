# Command cheat-sheet — automated 6-hourly UPAg Dash ingestion

All commands assume you are in the project folder:

```powershell
cd "C:\Users\syamm\OneDrive\Desktop\Onion Forecastijng intelligence agent\Onion-Forecasting-Intelligence-Agent"
```

## 1. Daily-use commands (the job: `src\data_layer\pull_upag_dash.py`)

| Purpose | Command |
|---|---|
| Look without touching anything (zero DB writes) | `.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --dry-run --json` |
| Run the ingestion now (yesterday's data) | `.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --once --json` |
| Re-check last N days (source published late) | `.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --once --days 3 --json` |
| Explicit date window | `.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --once --from 2026-10-21 --to 2026-10-27 --json` |
| Only some states | `.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --once --states MP --json` |
| Full-history reconciliation (2022 → yesterday) | `.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --once --full-history --json` |

Window rule: every run requests **yesterday = today (Asia/Kolkata) − 1**; the 4 runs per day
(00:00 / 06:00 / 12:00 / 18:00) re-check the same date — first inserts, rest skip or correct.

## 2. Scheduled task (`OnionUPAgDash6h`)

| Purpose | Command |
|---|---|
| Install the 6-hourly automation | `.\scripts\setup_scheduler_6h.bat` |
| Remove it | `schtasks /delete /tn "OnionUPAgDash6h" /f` |
| Check status + next run time | `schtasks /query /tn "OnionUPAgDash6h" /fo LIST` |
| Trigger a run right now | `schtasks /run /tn "OnionUPAgDash6h"` |
| Pause / resume without uninstalling | `schtasks /change /tn "OnionUPAgDash6h" /disable` / `/enable` |
| Run the backend (enables the task while the server runs, disables on exit) | `.venv\Scripts\python.exe src\api\backend.py` |

## 3. API smoke checks

```powershell
# Backend health (open route, no token) — expect store_engine "mongodb" and model_loaded true
curl.exe http://127.0.0.1:8000/health

# Data/ingestion health (requires the bearer token)
curl.exe -H "Authorization: Bearer <API_BEARER_TOKEN>" http://127.0.0.1:8000/api/ingestion/status
curl.exe -H "Authorization: Bearer <API_BEARER_TOKEN>" http://127.0.0.1:8000/api/data/status
```

## 4. Verification

```powershell
# Latest 3 dataset rows + their source label
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src/data_layer'); import history_store; [print(dict(d)) for d in history_store.get_db().observations.find({}, {'_id':0,'timestamp':1,'AP_retail_rs_per_metric_quintal':1,'source':1}, sort=[('timestamp',-1)], limit=3)]"

# Last 3 audit entries (one row per run)
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src/data_layer'); import history_store; [print(dict(d)) for d in history_store.get_db().ingestion_log.find({}, {'_id':0,'checked_at':1,'source':1,'status':1,'inserted':1,'unchanged':1}, sort=[('id',-1)], limit=3)]"

# One-time: create MongoDB indexes + backfill version:1 on pre-migration docs
.venv\Scripts\python.exe scripts\init_mongodb_indexes.py

# One-time: rename plan-alias collections (daily_prices/monthly_balances/ingestion_logs)
# to the canonical schema names if your dump used them
.venv\Scripts\python.exe scripts\align_collection_names.py

# One-time: rebuild counters from existing maxima (run after a restore)
.venv\Scripts\python.exe scripts\bootstrap_counters.py

# One-time: rebuild every mapped collection from the retained SQLite backup
# (destructive to MongoDB content; guarded by --yes)
.venv\Scripts\python.exe scripts\restore_from_sqlite_backup.py --yes

# One-time: compare retained SQLite backup with the MongoDB store (read-only parity report)
.venv\Scripts\python.exe scripts\verify_migration.py

# One-time: regenerate the seed model-ready CSV from the retained SQLite backup
.venv\Scripts\python.exe scripts\export_observations_csv.py

# Recurring: confidence-reliability report (displayed score buckets vs realized
# +/-5% band hit-rates over persisted Graph 1/2 price forecasts; read-only)
.venv\Scripts\python.exe scripts\check_confidence_reliability.py

# Full offline test suite (runs against the throwaway onion_forecasting_test MongoDB DB; expect 84 passed)
.venv\Scripts\python.exe -m pytest -q
```

## 5. MongoDB backup & monitoring

```powershell
# Full logical backup (install MongoDB Database Tools; one folder per date)
mongodump --uri "<MONGO_URI from config\.env>" --db onion_forecasting --out data\backups\%DATE:~10,4%-%DATE:~4,2%-%DATE:~7,2%

# Restore into the live DB (after a failure) — drop first to avoid duplicates
mongorestore --uri "<MONGO_URI>" --db onion_forecasting --drop data\backups\<date>\onion_forecasting
```

- Enable **Atlas automated backups** (Continuous Backup / or scheduled snapshots on M0+: manual trigger via UI) as the primary recovery path; `mongodump` is the offline second copy.
- Slow-query profiling for diagnostics (temporary, increases write overhead while on):
  `mongosh "<MONGO_URI>" --eval "db.getSiblingDB('onion_forecasting').setProfilingLevel(1, { slowms: 250 })"`
- The pre-migration SQLite file is retained at `data\onion_forecasting_v4.sqlite3` (plus `.bak` copy) until one week of stable MongoDB operation; only then archive/remove it.

Other audit locations: `data\corrections_log.csv` (value corrections), `data\snapshots\`
(raw responses, newest 30 kept), `data\pull_upag_dash.lock` (overlap guard — safe to
delete if a run crashed and left a stale lock).

## 6. From-scratch setup (new PC, or .venv deleted)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt
Copy-Item config\.env.example config\.env   # then set MONGO_URI, API_BEARER_TOKEN, INGESTION_API_KEY
.venv\Scripts\python.exe -m pytest -q                        # expect 84 passed
.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --dry-run --json  # sanity check
.\scripts\setup_scheduler_6h.bat                             # reinstall automation
.venv\Scripts\python.exe src\api\backend.py                  # start API + enable the task
```

`requirements.txt` includes `torch` and `chronos-forecasting` — needed by the forecasting
API; the first model use downloads the Chronos-2 weights.

## 7. Troubleshooting quick map

| Symptom | Check |
|---|---|
| API call returns 401 | Add `Authorization: Bearer <API_BEARER_TOKEN>`; the value must match `API_BEARER_TOKEN` in `config\.env` (an unconfigured token makes the API return 503 for /api/* routes) |
| Task exists but runs fail | `.venv` present? `config\.env` present with `UPAG_DASH_ENABLED=1`? |
| `inserted=0` but data is missing | Raise catch-up depth: `--days 3` (mandi rows publish late) |
| One state failed, others ok | Per-state isolation is by design — see the per-state `error` in `--json` output and `ingestion_log` rows with `status='error'` |
| Source changed column names/UOMs | Pipeline fails loudly (never guesses); inspect the newest snapshot in `data\snapshots\` |
| Value was wrong, now source fixed it | Automatic: next run applies it as a correction + logs to `data\corrections_log.csv` |
