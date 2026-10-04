# FinChaya app

The FinChaya web application: mutual-fund and stock analysis (fund holdings,
fund and stock screeners, portfolio overlap, goal planner, distributor tools).

- **Backend:** Python / FastAPI (`api.py` plus the `*_api.py` routers)
- **Database:** PostgreSQL (via `psycopg`)
- **Front end:** plain HTML, CSS and JavaScript served by the same FastAPI app.
  There is no build step and no framework.
- **Data pipeline:** Python scripts run on a schedule (`nightly.sh`, `monthly.sh`)

This folder is the deployable app. It is **not** the marketing site that lives
at the repository root.

## Layout (flat on purpose)

Everything sits in one folder because the code imports its sibling modules by
name (`from portfolio_api import router`) and serves its pages relative to
`api.py`. Copy the folder's contents to a server directory as-is and it runs.
Do not move files into sub-folders without updating those imports.

| Group | Files |
|---|---|
| Web app | `api.py`, `portfolio_api.py`, `saved_portfolio_api.py`, `screener_api.py`, `distributor_api.py`, `admin_api.py`, `selector.py`, `invest_value.py` |
| Optional routers | `payments.py` (Razorpay billing), `brokers.py` (broker hand-off). Mounted only if they load; a failure costs only their own endpoints. |
| Pages | `index.html`, `plan.html`, `screener.html`, `portfolio.html`, `portfolios.html`, `overlap.html`, `suggest.html`, `clients.html`, `allocation.html`, `admin.html`, `privacy.html`, `terms.html`, `refund.html` |
| Shared assets | `finchaya.css`, `finchaya.js` |
| Scheduled jobs | `nightly.sh` (server, cron), `monthly.sh` (run by hand on a laptop) |
| Pipeline | `load_*`, `fetch_*`, `parse_holdings.py`, `promote_holdings.py`, `detect_nav_splits.py`, `score_*`, `build_*`, `backfill_*`, `map_benchmarks.py`, `indicators.py`, `check_stale.py`, `check_freshness.py`, `zerodha_login_with_auto.py` |
| Schema | `create_*.py` and `add_*.py` migrations, plus `benchmark_tables.sql`, `returns_table.sql`, `returns_source.sql` |
| Admin tools | `grant_admin.py`, `grant_premium.py` |

## Run locally

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt        # Windows: venv\Scripts\pip
cp .env.example .env                            # then set FINCHAYA_DB
venv/bin/uvicorn api:app --reload --port 8000
```

Open <http://127.0.0.1:8000/> for the site and <http://127.0.0.1:8000/docs> for
the interactive API reference. `/api/health` is a quick liveness check.

The API refuses to start if `FINCHAYA_DB` is not set.

## Configuration

All settings come from environment variables, loaded from a `.env` file next to
`api.py`. **`.env.example` lists every variable the code reads**, with comments.
Never commit a real `.env`.

Two switches worth knowing about, both **off by default**:

- `MF_PAYWALL_ENABLED` gates subscriber-only data. Turn it on only once sign-in
  works.
- `MF_SCORES_ENABLED` is a hard off for FinChaya scores and ranks, for everyone
  including subscribers.

## Deploying changes

1. Copy the changed files to the server's app directory.
2. **Backend change** (`*.py`): restart the service.
   **Front-end change** (`*.html`, `*.css`, `*.js`): no restart needed. Browsers
   cache these aggressively, so hard-refresh (Ctrl+Shift+R) to see the change.

`nightly.sh` expects the app at `/opt/mfapi` with a virtualenv at
`/opt/mfapi/venv`; adjust the paths at the top if yours differ.

## Database

Tables are created and altered by the `create_*.py` and `add_*.py` scripts. Each
documents itself in its header. Where the app's database login does not own a
table, those scripts take an `--admin <role>` option to run as the owner, and
several offer `--check` to report what would change without changing it.

Run a migration before deploying code that depends on it.

## Scheduled jobs

- **`nightly.sh`** (server, cron): AMFI schemes and NAV, NAV split detection,
  stock candles and technicals (weekdays), stock scores, fund health scores,
  trailing returns, category trend, rolling returns (Saturdays), then two
  freshness checks. Order matters; the header explains why.
- **`monthly.sh`** (laptop): after the 10th, once AMCs have published monthly
  portfolios. It needs the AMC spreadsheets, the benchmark reference sheets and
  an interactive Zerodha login, none of which are in this repository.

## What is deliberately not in this folder

- `.env` and anything holding credentials or tokens
- Data: AMC portfolio spreadsheets, benchmark reference sheets, database dumps,
  archives, exports, and the `kite_instruments.json` cache
- Logs and checkpoints
- Superseded versions of files, one-off diagnostic and test scripts, and
  internal working notes
