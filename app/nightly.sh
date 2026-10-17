#!/bin/bash
#
# nightly.sh -- the daily refresh, run by cron.
#
# WHAT RUNS HERE
#   1. AMFI schemes + NAV       (published ~11pm IST on business days)
#   2. NAV split detection      (before anything reads NAV as a series)
#   3. Stock list (NSE's equity list), candles (NSE's daily price file), then
#      technicals from them (market days only)
#   4. Stock scores                 (market days only)
#   5. Fund health scores       (recomputed off the latest stock scores)
#   6. Trailing returns         (needs NAV and benchmarks)
#   7. Rolling returns          (Saturdays only -- weekly is enough)
#   8. Freshness report + check (are the stages still in step?)
#
# WHAT CHANGED, AND WHY
#   This script used to end at step 2 and then jump to fund scores. The
#   two stock steps did not exist here at all -- the old header said to
#   run them by hand from a laptop, because fetch_technicals.py needed an
#   interactive Zerodha login.
#
#   That is no longer true. zerodha_login_with_auto.py now logs in
#   headless, with no keyboard, so the whole stock pipeline can run
#   unattended. That matters more than convenience: score_funds.py takes
#   its as-of date from MAX(as_of_date) in stock_score, so for as long as
#   nothing fed the stock tables, EVERY fund score on the site was pinned
#   to whatever date the last manual run left behind. In September 2026
#   that was five days, and every step in this script reported OK
#   throughout.
#
#   fetch_technicals.py v3 also keeps its candles in stock_ohlc_daily and
#   fetches only missing days, so this step costs about twelve minutes
#   rather than the hour it used to.
#
# UPDATE 5 OCT 2026 -- ZERODHA IS OUT OF THIS RUN
#   The Zerodha candle fetch stopped working in mid-September: it stored one
#   stock a night and then died without closing its log row, 16 times. This
#   script carried on regardless, so stock scores and fund scores were built
#   from 11 September data while every later step reported OK.
#
#   Candles now come from NSE's own daily price file (fetch_bhavcopy.py), and
#   fetch_technicals.py --from-store computes indicators from what is stored.
#   NSE's file matched 2,286 of 2,286 stored Zerodha candles exactly (open,
#   high, low, close, volume) and covers ~230 stocks Zerodha never delivered.
#   No broker login, no 2FA secret on the server, one small download a day.
#
#   The Zerodha code is still in fetch_technicals.py without --from-store, and
#   zerodha_login_with_auto.py is untouched. Nothing here calls them.
#
# WHY --fresh IS ALWAYS PASSED
#   fetch_technicals.py writes a checkpoint of completed symbols so an
#   interrupted run can resume. Without --fresh, the NEXT night reads
#   that file, finds every symbol already done, and exits 0 having done
#   nothing -- which is precisely how five days of stale scores went
#   unnoticed in August 2026. A silent success is the worst failure mode
#   there is, so the checkpoint is cleared every night on purpose.
#
#   The checkpoint mattered when a redo cost an hour. Now that a full
#   re-run is twelve minutes, losing it is cheap and the footgun is not.
#
# WHY THE STOCK STEPS ARE SKIPPED AT WEEKENDS
#   There is no file to fetch on a day the market was shut. NSE answers 404
#   for those, which fetch_bhavcopy.py treats as "holiday" and skips quietly,
#   so a weekday holiday costs one cheap request and no calendar is needed.
#   Weekends are skipped here only to keep the log short.
#
# WHY set -u BUT NOT set -e
#   If AMFI is down, we still want fund scores to run off yesterday's
#   NAV data. Each step reports its own status instead of the whole
#   run dying on the first hiccup.

set -u

cd /opt/mfapi || exit 1

PY=/opt/mfapi/venv/bin/python
LOG_DIR=/opt/mfapi/logs
mkdir -p "$LOG_DIR"

STAMP=$(date +%F_%H%M)
LOG="$LOG_DIR/nightly_$STAMP.log"

# Everything below goes to the log AND to stdout (which cron mails, if
# configured). tee keeps both.
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo "FinChaya nightly run -- $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "============================================================"

failures=0

run_step () {
    local name="$1"; shift
    echo ""
    echo "--- $name ---"
    local start=$SECONDS
    if "$@"; then
        echo "--- $name OK ($((SECONDS-start))s) ---"
    else
        echo "--- $name FAILED (exit $?) ---"
        failures=$((failures + 1))
    fi
}

# A step that was deliberately not run is NOT a step that passed. Said
# out loud in the log, so a reader six weeks from now does not have to
# work out why Saturday's log is shorter than Monday's.
skip_step () {
    echo ""
    echo "--- $1 SKIPPED -- $2 ---"
}

# 1 = Monday ... 6 = Saturday, 7 = Sunday
DOW=$(date +%u)

run_step "AMFI schemes and NAV" $PY load_mf_schemes.py
run_step "NAV split detection"  $PY detect_nav_splits.py

# Dividends, splits, bonuses, rights and results dates. Every night, seven days a
# week: companies announce these at any time, and the "coming up" lists on the stock
# page and in the stock screener read this table. --bulk-only is a handful of
# requests (about a minute); the slow per-stock announcement history is not part
# of it. This used to be run by hand and had not run since 28 Sep, so six of the
# seven companies reporting on 9 Oct were missing. result_watcher.py refreshes the
# results dates again every 30 minutes by day.
run_step "Upcoming events (dividends, splits, bonus, rights, results dates)" $PY fetch_stock_events.py --bulk-only

# Shareholding patterns: who owns each company, from the quarterly filing every company makes with NSE.
# ONE list request covers the last 180 days, which catches new filings AND revisions (a revision keeps its
# original submission date, so a short window would never see it), then up to 400 filings' XBRL files are
# read for the breakdown (mutual funds, foreign institutions, individuals, pledged shares). Needs
# create_stock_shareholding_table.py run once first; until then this step fails and is counted, which is
# the reminder. Companies file within 21 days of a quarter end, so most arrive in the 3 weeks after
# 30 Jun, 30 Sep, 31 Dec and 31 Mar; on other nights this is a single quiet request.
run_step "Shareholding patterns (NSE filings)" $PY fetch_shareholding.py --days 180 --detail --detail-limit 400

# ---------------------------------------------------------------------
# THE STOCK SIDE.
#
# Ordering is not cosmetic. score_stocks.py reads stock_technical, and
# score_funds.py reads stock_score -- so these two must finish BEFORE
# fund scores, or the fund scores are computed against yesterday's view
# of the market and still report success.
# ---------------------------------------------------------------------
# Three steps in a fixed order, each feeding the next:
#   1. load_stock_master.py   the list of stocks. A stock that lists tomorrow
#                             is added tonight. (It used to run monthly, by
#                             hand, and had failed since 11 Sep on one symbol
#                             clash, so nothing new had been added in six
#                             weeks. It now tolerates a clash and reports it.)
#   2. fetch_bhavcopy.py      candles for every stock in that list, from NSE's
#                             daily file, filling any missing day.
#   3. fetch_technicals.py --from-store
#                             indicators, from the stored candles.
# If one fails the next still runs, off what is already stored, and the
# failure count at the bottom says so -- it does not pass quietly.
if [ "$DOW" -le 5 ]; then
    run_step "Stock list (NSE equity list)"   $PY load_stock_master.py
    run_step "Stock candles (NSE daily file)" $PY fetch_bhavcopy.py
    run_step "Stock technicals"      $PY fetch_technicals.py --from-store --fresh
    run_step "Stock scores"          $PY score_stocks.py
else
    skip_step "Stock list (NSE equity list)"   "weekend, market was shut"
    skip_step "Stock candles (NSE daily file)" "weekend, market was shut"
    skip_step "Stock technicals"               "weekend, market was shut"
    skip_step "Stock scores"                   "weekend, market was shut"
fi

run_step "Fund health scores"   $PY score_funds.py
run_step "Trailing returns"     $PY score_returns.py

# The rising split, per category AND per fund, off the scores just
# written. It reads stock_score, so it has to come after the stock side.
#
# WHY IT IS IN THE NIGHTLY RUN AT ALL
#   It was not, and that was a hole. The per-fund rows it keeps
#   (mf_fund_trend) are the only record of what a fund's rising score
#   WAS -- every page computes today's figure live and discards it. An
#   alert like "this has fallen for three months" reads that record and
#   nothing else.
#
#   backfill_fund_trend.py can rebuild the past from stored scores, so a
#   gap is recoverable rather than lost. But recovering a gap is work
#   nobody schedules, and a history with holes in it makes "three months
#   running" quietly mean "the last three readings, whenever those were".
#   Cheaper to never have the gap.
run_step "Category and fund trend" $PY build_category_trend.py

# Weekly, on Saturday night, off Friday's closing NAV. Rolling windows
# move slowly enough that a daily rebuild is work for no new answer.
if [ "$DOW" -eq 6 ]; then
    run_step "Rolling returns"  $PY score_rolling.py
else
    skip_step "Rolling returns" "runs Saturdays only"
fi

# ---------------------------------------------------------------------
# Last, so they judge the state the run actually left behind.
#
# Two checks, doing different jobs. check_stale.py is the readable
# report -- every dated table, how far behind it is against its OWN
# cadence, anything dated in the future, and what batch_run_log says
# actually ran. It always exits 0; it is there to be read.
#
# check_freshness.py is the gate. It exits non-zero when stages
# disagree, so a frozen pipeline shows up in the summary line below
# instead of passing silently.
# ---------------------------------------------------------------------
run_step "Freshness report"     $PY check_stale.py
run_step "Freshness check"      $PY check_freshness.py

echo ""
echo "============================================================"
if [ "$failures" -eq 0 ]; then
    echo "Nightly run complete -- all steps OK"
else
    echo "Nightly run complete -- $failures step(s) FAILED"
fi
echo "Finished $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "============================================================"

# Keep a fortnight of logs. Without this the directory grows forever
# and you find out when the disk fills. Hand-run fetch logs are pruned
# too -- they are the same kind of thing and nothing else cleans them.
find "$LOG_DIR" -name "nightly_*.log" -mtime +14 -delete
find "$LOG_DIR" -name "tech_*.log"    -mtime +14 -delete

exit "$failures"
