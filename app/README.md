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
| Admin tools | `grant_admin.py`, `grant_premium.py`, `grant_scores.py`, `referral_report.py` |

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
- `MF_SCORES_ENABLED` is the public switch for FinChaya scores, ranks and the
  rising/sideways/falling split. While it is off, nobody sees them, except
  accounts you add by hand with `grant_scores.py` (see below).

### Showing scores to a chosen few

With the public switch off, `grant_scores.py` shows scores to specific accounts
only. It keeps a short allowlist in its own table, `mf_scores_access`, which it
creates on first run, and it is run from a terminal on the server, never from
the site.

```bash
venv/bin/python3 grant_scores.py --subscribers                 # who subscribes, who has scores
venv/bin/python3 grant_scores.py --email a@x.com,b@y.com --note "beta 1"
venv/bin/python3 grant_scores.py --list
venv/bin/python3 grant_scores.py --revoke a@x.com
venv/bin/python3 grant_scores.py --revoke-all --yes
```

No restart is needed; the API re-reads the list about every 30 seconds. The list
only adds people. It does not bypass the paywall, so with `MF_PAYWALL_ENABLED`
on, a listed account that is not an active subscriber still sees the locked
version. If the table is missing or the database is unreachable, nobody is on
the list.

### Referrals

Signed-in members get a personal link (`/plan?ref=CODE`) and a ready-written
message to share by WhatsApp, text message, email or copy. A friend who opens
the link and signs up for the first time is credited to the member. There are
no rewards and FinChaya sends nothing on anyone's behalf. The code lives in
`referral_api.py`; the share dialog is in `finchaya.js`. The two tables it
needs (`referral_code`, `referral`) are created on first use. See who has
referred whom with `venv/bin/python3 referral_report.py`.

### Company results

`result_watcher.py` catches quarterly results the day they are filed. Each run
asks BSE's announcements feed who filed results today and yesterday, reads the
filing for the ones in our stock list, and stores revenue, profit and EPS in
`stock_financials`. It works out IST itself and an idle run is about three web
calls, so cron can run it every 30 minutes all day:

```
*/30 * * * * cd /opt/mfapi && venv/bin/python3 result_watcher.py >> logs/result_watcher.log 2>&1
```

Useful options: `--dry-run`, `--symbols TCS --days 400`, and
`--backfill 2026-06-30` to fill one past quarter for every stock still blank.
The same run also keeps the "results due" dates in the events table current (the
Corporate events tab and the stock screener's "upcoming results" list), from BSE's
and NSE's board-meeting calendars, and drops an old date when a meeting is moved.
`nightly.sh` runs `fetch_stock_events.py --bulk-only` every night for dividends,
splits, bonuses, rights and results dates.

A loss is printed in brackets, and some filings leave the minus sign off the tag; before
9 Oct 2026 the reader dropped or mis-signed those EPS lines. `result_watcher.py --fix-eps`
re-reads only the loss-making periods that were stored with no EPS or a positive one
(it can be sliced with `--shard`).

Each stored result also carries the day it was filed with the exchange (`filed_on`,
shown on the stock page as "Filed 8 Oct 2026"). Add the column once with
`venv/bin/python3 add_stock_financials_filed_on.py`, then fill in the dates for results
already stored with `result_watcher.py --stamp-dates` (no pages are downloaded). New
results get their date as they are read.

To load history, `--backfill-from 2025-03-31` fills every quarter and year since
that date in one pass; add `--shard 1/4` (and 2/4, 3/4, 4/4 in separate runs) to
share the work. Revenue that jumps ten-fold against the period before is held
back and listed, not written, because that is what a units slip looks like.

### Quarterly results page (`/results`)

A page of results by week (Monday to Sunday, Indian time): **this week** (filed so far, and who is still due), **last
week**, and **upcoming** (the weeks after). A card per company that has filed: Sales, Operating profit, Net profit and
EPS for the quarter, the quarter before and a year earlier, with the change against each; market cap; P/E and PEG with
an arrow for which way each moved. The code is `results_api.py` (the `/api/results` endpoint and the arithmetic) and
`results.html` (the page); `enable_results.py` switches it on in the server's `api.py` with one appended block.

- It reads `stock_financials` (filed_on picks the week), `stock_event` (results dates) and the latest close, and gets
  P/E, PEG and market cap from `build_ratios()` in `api.py`, the same working as the stock page.
- The P/E and PEG arrows compare the ratio now with the ratio before these results, both at today's price, so they move
  only because earnings changed. They show direction, not good or bad.
- For Stocks subscribers and administrators only (`stock_access.py` checks every call and ignores the paywall switch, so it
  is shut to non-subscribers even while the rest of the site is free). `MF_RESULTS_ACCESS=signed_in|everyone|admin` changes
  the audience; `MF_RESULTS_ENABLED=false` switches it off. Answers are cached for a minute.
- **Operating profit** (sales minus costs before interest and depreciation; other income is not counted) was never
  stored. Add the two columns once with `venv/bin/python3 add_stock_financials_operating.py`, then
  `venv/bin/python3 result_watcher.py --operating` reads them for the results of the last 30 days (three filing pages per
  stock). From then on the watcher stores them with each new result and for the two quarters it is compared with. Banks,
  finance companies and insurers get profit before tax instead. Until then the row shows dashes; nothing else breaks.
- Steps, and what to check afterwards: `DEPLOY_RESULTS.md`.

### Stock alerts (`/stock-alerts`, and "Set alert" on a stock page)

A member picks a stock and what to be told about: a price level; a close above (below) the highest high (lowest low) of the
last N sessions or the high (low) of a chosen date; the price coming down to an EMA (an early heads-up, or "dips to it and
holds"); a Dow-theory change of trend on the daily, weekly or monthly chart (the same measure as the "Change in Dow Theory"
screens); any technical indicator condition from the trade-plan catalogue; or the next quarterly results (optionally only if
profit / sales / EPS moved a stated way on a year ago) or the announcement of the results date. One email, once.

- `stockalerts_engine.py` is the pure part (validation, one-sentence wording, the check, the email text); `stockalerts_api.py`
  the endpoints under `/api/stockalerts` and the two static routes; `stockalerts_checker.py` the scheduled check (cron every
  30 minutes) and CLI (`--dry-run`, `--status`, `--test-email`); `stockalerts.js` the "Set alert" window; `stockalerts.html`
  the My alerts page; `create_stock_alert_tables.py` the tables; `enable_stockalerts.py` the append-only switch-on.
- Honest by construction: end-of-day data only, so price alerts are judged on a **close**; weekly/monthly wait for the candle to
  finish; a level already crossed is refused at creation; a trigger is recorded **before** the email is tried and retried up to
  six times; a price level is paused if a split or bonus follows; the page reports whether email and the checker are really set up.
- Stocks subscribers and administrators only (`stock_access.py`; `MF_STOCKALERTS_ACCESS`, `MF_STOCKALERTS_ENABLED`).
- **Email** for alerts, trade-plan alerts and payment receipts is `mailer.py`: Resend over HTTPS (`MF_MAIL_PROVIDER=resend`,
  `RESEND_API_KEY`, `MF_MAIL_FROM`; this is what works from the droplet) or SMTP (`MF_SMTP_USER`, `MF_SMTP_PASS`, which DigitalOcean
  blocks). `python mailer.py --diagnose` and `--test you@example.com`.
- The back-test (`/backtest`) is now part of the Stocks plan (`MF_BACKTEST_ACCESS`, default `premium`) and has an "ask us for an
  indicator" box (`feature_requests.py`, table `indicator_request`).


### Price chart, ratios and shareholding (stock page)

- **Price chart** (`/api/stocks/{isin}/prices`). Daily closes from `stock_ohlc_daily`, divided back for
  splits and bonuses, but only where the prices themselves confirm the drop (the stored history is a mix of
  adjusted and unadjusted rows, so nothing is assumed). History before 1 Aug 2024 came from Zerodha Kite, whose
  terms restrict showing it publicly, so the chart starts on that date unless `MF_CHART_FULL_HISTORY=1` is set.
  `fetch_stock_events.py --actions-history-from 2024-08-01` loads NSE's split/bonus/dividend history (one-off).
- **Ratios** (`/api/stocks/{isin}/ratios`): P/E, PEG, P/B, book value, market cap, return on equity, debt to
  equity, interest cover. Each comes with its working or the reason it is missing. The balance-sheet ones read
  `stock_balance_sheet`: run `create_stock_balance_sheet.py` once, then `result_watcher.py --balance-sheet`
  (with `--shard K/N` to share the work) for history; new annual, March and September results store theirs as
  they are read.
- **Shareholding** (`/api/stocks/{isin}/shareholding`) from NSE's quarterly filings: run
  `create_stock_shareholding_table.py` once, then `fetch_shareholding.py --history-quarters 12` and
  `fetch_shareholding.py --detail-only --detail-since 2026-03-31 --detail-limit 0` for the first load.
  `nightly.sh` keeps it current (`--days 180 --detail --detail-limit 400`).

- **Ownership screeners** (`ownership_screens.py`, mounted by `screener_api.py`): FIIs and mutual funds
  buying or leaving, both together, institutions buying while the price stands still, rising quarter after
  quarter, promoters buying or reducing, promoter pledge, institutions against individuals. They compare
  one quarter's shareholding pattern with the one before; a move that is just a company re-labelling holders
  is set aside and counted. Needs `stock_shareholding` with the breakdown loaded for two quarters.

- **Chart screens** (`price_bars.py`, `dow_change.py`, `fib_retracement.py`, mounted by `screener_api.py`):
  - *Change in Dow Theory*, turning up (a downtrend becoming an uptrend: a break above the fall's last lower
    high, a pullback that holds a higher low, then a second higher high) and turning down (the mirror, run on
    the flipped prices so both follow the same rules).
  - *Fibonacci retracement (62-78%)*: the lowest point in the look-back window, the highest point after it, and
    the price now within about 61.8-78.6% of that rise (2 points of leeway by default).
  - Each can read a **line** (closing prices only) or **candlesticks** (highs and lows); the stock page's price
    chart has the same Line / Candles choice, kept in the reader's browser.
  - `price_bars.py` reads the daily candles once, builds weekly and monthly bars from them (the current period as
    it stands), holds back bad ticks, sets aside stocks with an unadjusted split and caches the result per trading
    day. `add_ohlc_date_index.py` is an optional index that may shorten the first read.

- **Expanded flat** (`expanded_flat.py`, two chart screens): an uptrend, a pullback 0 to A, a higher high B at 1.15-1.25
  times the 0-to-A move above A, then C about 1.6 times that move below B (below A), within 60 bars; and the mirror in a
  downtrend (run on the flipped prices). Daily, weekly or monthly, line or candlesticks. Ratios are targets with a "How
  exact" give. Reads bars from `price_bars.py` and swings from `dow_change.py`.

- **Stock price chart** (`index.html`): Daily / Weekly / Monthly bars (grouped in the browser from the daily bars) and Line /
  Candles, with the table under it following the choice.

- **Snapshot tab** (fund and stock pages, first tab, `index.html` plus `snapshot_api.py`): a one-page summary of cards, each
  linking to the tab that holds the detail. Everything comes from endpoints the pages already call, except a fund's
  calendar-year returns and rank in category (`/api/snapshot/fund/{code}/years`, computed from NAV history). No score and
  no good/bad colouring: each figure sits beside what the category's middle fund did.

### Newer additions (October 2026)

- **IPO section** (`/ipo`, `/ipo/<slug>`, and an IPO tab on a listed company's stock page): upcoming, open, listed and SEBI-draft issues read from the
  offer documents, with 14 ready-made scanners. Information only, no verdict. `ipo_api.py`, `ipo_refresh.py` (daily job), `ipo_view.js`,
  `ipo.html`, `ipo_issue.html`; tables from `create_ipo_tables.py`. IPO trend screen and its email alert: `ipo_trend.py`, `ipo_alerts.py`.
- **Chart-pattern scanners** (`chart_patterns.py`, mounted by `screener_api.py`): double top and double bottom (with the fake breakout / breakdown
  rule), head and shoulders and its inverse, flag and pole (bullish and bearish). Every signal is a close, never a wick, and is worked out from the bars
  up to each day only, so the scanner, the alert and the back-test agree. They sit on the "Chart patterns" tab; RSI, MACD, crossovers, Dow theory,
  Fibonacci, higher low, expanded flat, multi-year breakout and IPO trend sit on "Indicator and concept based".
- **Scanner alerts** (`scanner_alerts.py`, table script `create_scanner_alert_tables.py`; cron every 30 minutes): "email me when a new stock appears"
  on any scanner. **Back-test this scanner** (`scanner_signals.py`, `backtest_engine.py`, `backtest.html`) replays a scanner as a rule in the back-tester.
- **My trade plan, Check your view, Build your own screener** (`tradeplan_*`, `viewcheck_api.py`, `screenbuilder_*`): your own AND / OR rules over charts,
  results, ratios, ownership and fund flows.
- **Free trial** (`trial.py`, `create_trial_table.py`): seven days of one plan with no card; a reminder email on day 6 and a farewell on day 8
  (`trial.py --run` from cron, `--dry-run`, `--status`, `--sample`). One trial per person.
- **Plan alerts** (`portfolio_monitor.py`, `create_portfolio_monitor_tables.py`): a monthly check of a locked plan and three alerts. Needs email set up.
- **Phones**: the header turns into a Menu button on small screens (`finchaya.js` / `finchaya.css` for every page except the home page, which has its
  own in `index.html`).

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
- Superseded versions of files, one-off diagnostic scripts, and internal working notes
- The automated tests and QA tools (`*_demo/` folders, `test_*.py`, `qa/`): they connect to a local scratch database with a login of their own
  and are kept out until that is moved into environment variables
- Runbooks that name servers and key paths (`DEPLOY_*.md`)
