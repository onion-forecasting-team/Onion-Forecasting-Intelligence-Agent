# Onion Forecasting MVP — Chronos-2 + FastAPI + Streamlit

This project is a local, runnable MVP using separate daily price and AP monthly quantity datasets. Graph-specific observations are synchronized into MongoDB before forecasting.

## Documentation

- [`docs/API.md`](API.md) — full endpoint/JSON contract for all five graphs, forecast persistence, ingestion, CORS, and auth.
- [`docs/CHATBOT_INTEGRATION.md`](CHATBOT_INTEGRATION.md) — chatbot integration contract.
- [`docs/MONGO_MIGRATION.md`](MONGO_MIGRATION.md) — end-to-end MongoDB migration record and ops/rollback runbook.
- [`docs/COMMANDS.md`](COMMANDS.md) — command cheat-sheet for ingestion, scheduler, verification, and backups.

## What is included

- **Graph 1 API/UI:** Forecasts AP retail, wholesale, and mandi modal prices using the specified AP price and arrival history. Supports two independently retrievable data sources selected by the `source` parameter (`upag` live pipeline — default —, `avaghana` static district-derived state-average retail snapshot with a pre-generated six-month forecast stored in `Avaghana_AP_Retail_Dataset`/`AVAGHANA_AP_FORECAST`, or `both` for a single-graph comparison). The backend never regenerates the Avaghana forecast; refreshing it means re-importing newer data with `scripts/import_avaghana.py`.
- **Graph 2 API/UI:** Forecasts retail, wholesale, and mandi modal prices independently for AP, MH, KA, and MP using only each selected state's matching price and arrival history.
- **Graph 3 API/UI:** Forecasts AP monthly production, consumption, and arrivals in tonnes/month, then derives shortfall as `consumption - (arrival + production)`.
- **Graph 4 API/UI:** Forecasts AP monthly retail prices (₹/kg) over the monthly dataset, with optional wholesale/mandi price and supply-demand features as covariates.
- **Graph 5 API/UI:** Serves pre-computed district-wise retail price forecasts for AP districts (daily district history back to January 2020 plus a 180-day district forecast grid); no model inference on the request path.
- **Chatbot:** Read-only Groq tool-calling assistant (`POST /api/chat`, SSE `POST /api/chat/stream`) that reuses the same graph handlers; the LLM may only quote numbers returned by its tools.
- **MongoDB history store:** Automatically synchronizes the configured daily price CSV and monthly AP CSV into separate graph-specific MongoDB collections. Source changes are reloaded on the next graph request. The existing full-schema ingestion store (now backed by MongoDB) remains available for its existing ingestion workflows. The pre-migration SQLite file is retained read-only as a backup for `scripts/verify_migration.py`.
- **Forecast serving and persistence:** Graph 1/2 responses are served DB-first — sliced from the latest precomputed 180-day MongoDB grid run, regenerated automatically by a background scheduler. Chronos-2 runs on the request path only as the fallback. Every saved run is retrievable via `/api/forecasts/latest` and `/api/forecasts/runs`.
- **UPAg discovery utility:** Uses credentials from local `.env`, logs into the official UPAg Open API, lists the source names permitted for that account, and can save raw JSON responses when a source-specific request payload is provided.
- **Mocked API tests:** Check request/response structure (five graph endpoints plus chat, ingestion, and CORS flows) without downloading Chronos-2 model weights.

## Important honesty/limitations

1. The actual Chronos-2 model has to be installed and its weights downloaded on your machine. The mocked tests do **not** claim real inference was executed here.
2. The configured daily price dataset starts 2022-01-01 and is extended automatically to the most recent day by the 6-hourly UPAg Dash ingestion (see "Automated 6-hourly UPAg Dash ingestion" below). The monthly AP CSV ends in **October 2026** (58 months, January 2022–October 2026). Forecast results use the source's daily or monthly frequency.
3. The supplied CSV does not contain weather. This MVP does not yet claim to forecast under El Niño/cyclone scenarios.
4. Graph 3 quantities are monthly totals supplied by the BA team. Validate the definitions of production, consumption, and arrivals before interpreting the resulting shortfall.
5. Shortfall quantile bounds are conservative combinations of individual component quantiles, not a calibrated joint interval.
6. UPAg credentials alone do not prove that a source provides the fields required by the model. First list the sources authorized to the account and inspect the exact response schema. The included UPAg utility fetches **raw data only**; it does not silently map unknown source columns into the model-ready dataset.
7. The Avaghana Graph 1 source is a static snapshot (history 2020-01-01 → 2026-11-03 plus a pre-generated six-month forecast). It is an equally weighted district average, not a volume-weighted official state average; the stored forecast is median-only (no P10/P90), so Avaghana rows carry no prediction-interval fields and no tolerance-band confidence score, and the price unit still needs BA-team confirmation (the dataset's own Methodology sheet flags it).

## Recommended environment

Use **64-bit Python 3.11** for the MVP. The first real forecast requires internet access to install dependencies and download the public Chronos-2 model weights. CPU inference works but can be slower than a supported CUDA GPU.

## Windows setup

1. Copy or extract the project into its own folder, for example `C:\work\onion-forecasting-agent`, and open a terminal at the project root. The project ships with a ready `.venv`; to rebuild it from scratch run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, use Command Prompt and run `.venv\Scripts\activate.bat`, or execute `.venv\Scripts\python.exe` directly.

2. Create the environment file from the template and fill in the required values (`MONGO_URI` for the MongoDB store, plus `API_BEARER_TOKEN` and `INGESTION_API_KEY`):

```powershell
Copy-Item config\.env.example config\.env
```

3. Start the API in terminal 1 (this launcher also enables the scheduled 6-hourly ingestion task while the server runs):

```powershell
.\.venv\Scripts\python.exe src\api\backend.py
```

4. Open `http://127.0.0.1:8000/health` to verify the API, model, and MongoDB store (`store_engine: "mongodb"`, `model_loaded: true`). Then open `http://127.0.0.1:8000/docs` (Swagger) or `/redoc`.

5. Start the dashboard in terminal 2:

```powershell
.\.venv\Scripts\python.exe -m streamlit run src\ui\dashboard.py
```

6. Open the local Streamlit address printed by the terminal, usually `http://localhost:8501`.

**Chronos-2 weights are pre-warmed at server startup.** Keep the backend terminal open until the `Pre-warming Chronos-2 pipeline` log line finishes; do not repeatedly click Generate while the first forecast after a restart is running.

## Frontend integration

See [`docs/API.md`](API.md) for the JSON contract, supported parameters, request headers, CORS guidance, and examples, and [`docs/CHATBOT_INTEGRATION.md`](CHATBOT_INTEGRATION.md) for the chatbot contract.

## Direct API checks

All `/api/*` routes require `Authorization: Bearer <API_BEARER_TOKEN>` (the only exceptions are CORS preflight `OPTIONS` calls and `POST /api/ingestion/sync-csv`, which uses `X-Ingestion-Key`). `/` and `/health` are open.

```text
GET http://127.0.0.1:8000/api/graphs/ap-retail?start_date=2026-10-01&end_date=2027-02-01&price_types=retail
GET http://127.0.0.1:8000/api/graphs/ap-retail?start_date=2026-10-01&end_date=2027-02-01&price_types=retail,wholesale,mandi_modal

GET http://127.0.0.1:8000/api/graphs/state-comparison?states=AP,MH&price_types=retail,wholesale&start_date=2026-10-01&end_date=2027-01-01
GET http://127.0.0.1:8000/api/graphs/state-comparison?states=KA,MP&price_types=retail,wholesale&start_date=2026-10-01&end_date=2027-01-01

GET http://127.0.0.1:8000/api/graphs/supply-balance?states=AP&start_date=2026-10-01&end_date=2027-01-01

GET http://127.0.0.1:8000/api/graphs/monthly-retail?states=AP&start_date=2026-07-01&features=retail,arrival

GET http://127.0.0.1:8000/api/graphs/district-retail?districts=Guntur,Kurnool

GET http://127.0.0.1:8000/api/forecasts/latest
GET http://127.0.0.1:8000/api/forecasts/runs?limit=20
GET http://127.0.0.1:8000/api/data/status
GET http://127.0.0.1:8000/api/ingestion/status
```

Graphs 1–3 require both `start_date` and `end_date`; Graph 4 requires `start_date`; Graph 5 requires `districts` (dates optional, defaulting to the full covered range). For optional parameters per endpoint (`price_types`, `features`, district selection limits, truncation behavior beyond the 180-day cap), see [`docs/API.md`](API.md) §1.

## History store and CSV updates

The existing full-schema ingestion store is initialized from its configured legacy CSV when required. Graph requests separately synchronize the new daily and monthly datasets into graph-specific collections in the configured MongoDB database (`MONGO_DB_NAME`, default: `onion_forecasting`; connection via `MONGO_URI` in `config/.env`). Collections mirror the former SQLite tables 1:1 (same field names); the legacy `data/onion_forecasting_v4.sqlite3` file is retained only as a verified backup.

> **Migrating from SQLite?** See [`MONGO_MIGRATION.md`](MONGO_MIGRATION.md) for the end-to-end record: why the store was converted, the techniques used (upserts, counters, aggregation pipelines, index plan), the verification/parity methodology, and the ops/rollback runbook.

Graphs 1 and 2 instead read the daily dataset configured by `ONION_DAILY_PRICE_DATA_PATH`; Graph 3 reads the monthly AP dataset configured by `ONION_AP_MONTHLY_DATA_PATH`. The supplied defaults point to the two CSV paths provided for this change. Set these variables in `.env` if the files are stored elsewhere. The API validates each dataset and synchronizes it into the `daily_price_observations` or `ap_monthly_observations` collection in the MongoDB store. These graph endpoints do not use the legacy combined model-ready CSV as their observation source.

If you receive a newer **full combined model-ready** CSV, put it inside `data/` and import it:

```powershell
.\.venv\Scripts\python.exe src\data_layer\sync_model_ready_csv.py data\Onion_Model_Ready_Dataset_Chronos2_updated.csv
```

It is an upsert keyed by date; it will not duplicate dates. Supply the full aligned history, not a file containing only the latest row. A row must contain the expected AP/KA/MH/MP fields so the combined date can be validated. After the import, the API reads the new database history and forecast cache keys change automatically.

There is also a secured local admin endpoint for a CSV already placed inside `data/`:

1. Change `INGESTION_API_KEY` in `.env` to a long, unique secret.
2. Place the normalized combined CSV under `data/`.
3. Call `POST /api/ingestion/sync-csv?filename=your_file.csv` with request header `X-Ingestion-Key: <your-secret>`.

Do not expose the local ingestion endpoint to the public internet.

## UPAg Open API discovery

Official docs: https://data.upag.gov.in/docs and https://data.upag.gov.in/redoc

The documented API uses a login at `/v1/upag/api-data-share/login`, then lists account-authorized data sources at `/v1/upag/api-data-share/sources/user-allowed-sources`, and fetches a source with `POST /v1/upag/api-data-share/sources/{source_name}`. Source request objects differ by source.

1. Put the account's authorized credentials in `.env` (`UPAG_USERNAME`, `UPAG_PASSWORD`, plus client ID/secret only if your account documentation requires them). Do not share passwords, tokens, cookies, or `.env`.
2. Run:

```powershell
.\.venv\Scripts\python.exe src\data_layer\upag_client.py --list-sources
```

3. Confirm whether `agmarknet` and/or `doca` appear in `allowedSources`.
4. To retrieve one source, create a JSON payload using the exact source-specific request schema from the official docs and run:

```powershell
.\.venv\Scripts\python.exe src\data_layer\upag_client.py --source agmarknet --payload-file config\agmarknet_request.json
```

The raw response will be saved under `data/upag_raw_agmarknet.json`. The payload cannot be guessed: confirm field names/filters from Swagger and examine the response keys.

Before turning polling on, the next implementation step is a source adapter that maps the real UPAg response into the canonical daily dataset (date × state, retail price, mandi price, arrivals, and validated quantity series). Retail and mandi/arrival data may come from different sources and different geographic granularities; do not merge them by date alone until the state, unit, crop and market dimensions are verified. Share only the **source names, redacted request payload, and a few redacted response rows** so the adapter can be tailored. Never share account credentials.

## Automated 6-hourly UPAg Dash ingestion

`pull_upag_dash.py` keeps the MongoDB history and daily price CSV current. Every run:

1. Requests **yesterday's date** (Asia/Kolkata) from the public UPAg Dash MIP by-crop endpoint for all four states (AP, MH, KA, MP).
2. Extracts the four sourced series per state — **retail, wholesale and mandi modal prices (Rs/Qtl, unconverted)** and **mandi arrivals (Tonnes ×10 → metric quintals)**.
3. Appends dates that are missing, corrects single values that the source revised (logged in `data/corrections_log.csv`), and skips identical values — so the four runs per day are idempotent. Values are written to the legacy ingestion history, mirrored into `daily_price_observations` for the graph API, and synchronized into the configured daily price CSV.
4. Forward-fills fields not supplied by this API on new daily rows, matching how the "Interpolated" dataset was built. AP daily arrivals are also rolled up from quintals/day to tonnes/month in the matching `ap_monthly_observations` row; monthly production and consumption remain sourced from the configured monthly dataset. If the DB lags behind yesterday, the window auto-extends back to `DB_last + 1` and unfilled dates become `upag_dash_6h_gapfill` rows so the daily index never breaks.

Mapping provenance: every series was cross-checked against the ingested history on 2026-10-28 with anchor dates for all four states (e.g. AP 2022-01-01 retail 4000.0, MP 2023-06-18 wholesale 1205.45, MP arrivals 1219.35 T → 12193.5 qtl). A mismatch raises loudly and blocks ingestion (`dash_extract.ANCHORS`).

Run it manually:

```powershell
.\.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --dry-run      # fetch + compare, zero DB writes
.\.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --once        # append/correct now
.\.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --days 3      # catch-up back 3 days (late mandi rows)
.\.venv\Scripts\python.exe src\data_layer\pull_upag_dash.py --full-history # 2022-01-01..yesterday reconciliation
```

Schedule it (runs at 00:00 / 06:00 / 12:00 / 18:00 local time, survives API/dashboard restarts):

```powershell
.\scripts\setup_scheduler_6h.bat          # creates task "OnionUPAgDash6h"
schtasks /delete /tn "OnionUPAgDash6h" /f # removes it again
```

Backend-coupled alternative: `.\.venv\Scripts\python.exe src\api\backend.py` starts the
FastAPI server **and** enables the task only while the backend runs (disables it again
on Ctrl+C). The standalone install above remains the default and is independent of
the backend.

Every run is auditable in `ingestion_log`, and raw responses are snapshotted under `data/snapshots/` (newest 30 kept). Environment switches live in `.env` (`UPAG_DASH_ENABLED`, `UPAG_DASH_DAYS`, `UPAG_DASH_SNAPSHOTS`, `UPAG_DASH_UPDATE_EXISTING`, …). Lock file `data/pull_upag_dash.lock` prevents overlapping runs. Offline tests: `pytest tests\test_dash_ingestion.py`.

The scheduled job fetches UPAg Dash data and writes it to MongoDB and the configured daily price CSV; it does not call the local FastAPI HTTP API. The monthly source CSV is unchanged; AP monthly arrivals are refreshed only in the MongoDB graph collection, while monthly production and consumption remain sourced from the configured monthly dataset. The API reads the updated store when its data endpoints are requested. If an AP monthly row is not yet present, the job reports that its arrival total could not be updated; it does not invent monthly production or consumption values.

## Chronos-2 output interpretation

- P50 is the median forecast.
- P10–P90 is a nominal 80% prediction interval.
- `interval_width_pct_of_median` is a forecast spread indicator, **not a calibrated confidence score**.
- A reliability score requires rolling-origin backtests with actual held-out values for each horizon/state/price type.
- Graph 1/2 forecast rows include a **derived** `confidence_score` per day: the probability that the actual price lands within ±`ONION_CONFIDENCE_TOLERANCE_PCT` (default ±5%) of the forecast median, computed from the Chronos-2 P10/P90 spread after stretching it by the series' spread multiplier `k`. `k` is **backtest-calibrated per price series** (split-conformal, 0.80 two-sided distance-to-median coverage; evidence in `data/calibration/`): retail 1.266404 (raw coverage 79.42% → calibrated 90.20%), wholesale 1.084111 (87.21% → 89.66%), mandi modal 1.260045 (77.86% → 88.97%). Per-series env keys `ONION_CONFIDENCE_SPREAD_K_<SERIES>` override the single `ONION_CONFIDENCE_SPREAD_K`; `ONION_CONFIDENCE_K_CALIBRATED=1` records that the configured values are backtest-calibrated. Calibration reference point: when the P10/P90 distances equal the band width and `k=1`, the score is exactly the nominal 80. The score is a cumulative minimum across forecast dates within each series, so it can only stay level or fall with horizon; `k` only reshapes the score — returned P10/P50/P90 stay the raw Chronos-2 quantiles. `scripts/check_confidence_reliability.py` compares displayed score buckets against realized ±5% band hit-rates as the standing drift check.

## Run tests without model downloads

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite (3 test files, 91 tests) monkeypatches Chronos-2 and runs against a throwaway `onion_forecasting_test` database on the configured MongoDB store, verifying the five graph response shapes plus chat, ingestion, and CORS behavior. To confirm real forecasting, run the service and request one graph endpoint with the actual model installed.


## v4 graph-specific model logic and persisted forecasts

The daily graph dataset starts 2022-01-01 and is extended automatically by the 6-hourly UPAg Dash job; its prices are converted from INR/kg to INR per metric quintal for model context, while forecast outputs are converted back to INR/kg. Daily arrivals are converted from tonnes/day to metric quintals/day. The monthly AP dataset contains 58 months through October 2026 and retains its tonnes/month units. Graph 5 is served directly from the `district_wise_data` history and `forecast_district_wise_data` pre-computed collections, with no request-path inference.

Each price forecast includes its own historical target context. Retail inputs are same-state wholesale, mandi modal, and arrivals; wholesale inputs are same-state mandi modal and arrivals; mandi modal inputs are same-state arrivals. Unknown future covariates are independently forecast first. Graph 3 forecasts monthly production, consumption, and arrivals as separate univariate targets and derives `Shortfall = Consumption - (Arrival + Production)`. The model's 180-day maximum is preserved; longer requested ranges return forecasts up to the maximum supported date and flag the truncated range in the response. Forecasts are persisted to `forecast_runs` and `forecast_values` and retrievable via `/api/forecasts/latest` and `/api/forecasts/runs`. See `docs/API.md` and `docs/MONGO_MIGRATION.md`.
