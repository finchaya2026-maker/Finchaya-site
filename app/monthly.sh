#!/usr/bin/env bash
#
# monthly.sh -- the once-a-month FinChaya refresh.
#
# WHERE THIS RUNS
#   On the laptop, not the droplet. Three of these steps need things that
#   only exist here: the AMC .xlsx files, the CRISIL reference sheets, and
#   an interactive Kite login. nightly.sh on the droplet handles the parts
#   that can run unattended (NAV load, split detection, returns scoring).
#   Git Bash or WSL both work.
#
# WHEN TO RUN IT
#   After the 10th. AMCs must publish monthly portfolios by the 10th, so
#   running earlier means parsing last month's files again. A stock listed
#   on the 3rd cannot appear in a portfolio disclosed as at the previous
#   month end, so there is nothing to gain by going early.
#
# WHY THIS ORDER
#   Fund health scores are built from stock scores. A stock that appears in
#   a new portfolio but has not been fetched and scored yet drags that
#   fund's coverage percentage down. So the whole stock half of the pipeline
#   runs before the fund half -- that is what keeps coverage at 100%.
#
#   Within the fund half: parse -> promote -> score -> map -> returns.
#   map_benchmarks reads mf_score, so a fund is invisible to it until it has
#   been scored. score_returns reads mf_benchmark_map, so it must come last.
#
# USAGE
#   ./monthly.sh                 # everything
#   ./monthly.sh --skip-stocks   # funds only, if stocks already refreshed
#   ./monthly.sh --dry-run       # print the steps without running them

set -euo pipefail

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
BASE="/c/finchaya"
PY="python"
REFERENCE_DIR="benchmark mutual fund references"
PORTFOLIO_DIR="amc portfolios"
INSTRUMENT_CACHE="kite_instruments.json"
LOG_DIR="$BASE/logs"

SKIP_STOCKS=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --skip-stocks) SKIP_STOCKS=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$BASE"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/monthly_$(date +%Y%m%d_%H%M%S).log"

say() { printf '\n=== %s === %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG"; }

run() {
  say "$*"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  (dry run -- not executed)" | tee -a "$RUN_LOG"
    return 0
  fi
  # Fail loudly. A step that half-runs and gets ignored is how bad data
  # reaches the site.
  "$@" 2>&1 | tee -a "$RUN_LOG"
  return "${PIPESTATUS[0]}"
}

# ---------------------------------------------------------------------
# PREFLIGHT -- fail before doing work, not halfway through
# ---------------------------------------------------------------------
say "Preflight"

[ -d "$REFERENCE_DIR" ] || { echo "missing: $REFERENCE_DIR" >&2; exit 1; }
[ -d "$PORTFOLIO_DIR" ] || { echo "missing: $PORTFOLIO_DIR" >&2; exit 1; }

new_files=$(find "$PORTFOLIO_DIR" -name '*.xls*' -newermt '-35 days' | wc -l)
echo "  AMC files modified in the last 35 days: $new_files" | tee -a "$RUN_LOG"
if [ "$new_files" -eq 0 ]; then
  echo "  WARNING: no recent portfolio files. Did the downloads happen?" | tee -a "$RUN_LOG"
fi

# ---------------------------------------------------------------------
# STOCK HALF
# ---------------------------------------------------------------------
if [ "$SKIP_STOCKS" -eq 0 ]; then

  # New listings first -- nothing downstream can score a stock that has
  # no master row.
  run "$PY" load_stock_master.py

  # The instrument cache maps symbol -> Kite token. A stock added to the
  # master this month will not resolve against a cache built last month,
  # and it fails SILENTLY -- the stock is just skipped, exactly like the
  # STLTECH-BE case. Dropping it forces a rebuild. Only needed on the
  # monthly run; the weekly ones can reuse it.
  if [ -f "$INSTRUMENT_CACHE" ]; then
    say "Dropping $INSTRUMENT_CACHE so new listings resolve"
    [ "$DRY_RUN" -eq 1 ] || rm -f "$INSTRUMENT_CACHE"
  fi

  # Kite access tokens expire daily. A long run started on a stale token
  # dies on the first request, so check before spending an hour on it.
  run "$PY" zerodha_login_with_auto.py

  run "$PY" fetch_technicals.py
  run "$PY" score_stocks.py
fi

# ---------------------------------------------------------------------
# FUND HALF
# ---------------------------------------------------------------------
run "$PY" parse_holdings.py "$PORTFOLIO_DIR/*.xls*"

# promote_holdings takes the disclosure date, which is the PREVIOUS month
# end -- not today. Files published on the 10th of March carry February
# portfolios.
AS_AT=$(date -d "$(date +%Y-%m-01) -1 day" +%Y-%m-%d 2>/dev/null \
        || date -v-1m +%Y-%m-31)
say "Promoting holdings as at $AS_AT"
run "$PY" promote_holdings.py all "$AS_AT"

run "$PY" score_funds.py

# Only reaches funds that have a current health score, so it must follow
# score_funds. Reads the CRISIL sheets, which is why this cannot live on
# the droplet.
run "$PY" map_benchmarks.py "$REFERENCE_DIR"

# Reads mf_benchmark_map. Also runs nightly on the droplet; running it
# here refreshes returns immediately rather than waiting for tonight.
run "$PY" score_returns.py

# ---------------------------------------------------------------------
say "Done. Log: $RUN_LOG"
cat <<'EOF'

Worth eyeballing before you walk away:
  * benchmark_review.csv     -- funds still needing a decision
  * the coverage column on a few fund pages (should be 100%)
  * any "CHECK:" lines in the parse output above

Not done here -- they belong in nightly.sh on the droplet:
  load_nav.py, detect_nav_splits.py, score_returns.py
EOF
