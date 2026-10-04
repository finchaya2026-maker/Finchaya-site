#!/bin/bash
#
# nightly.sh -- the daily refresh, run by cron.
#
# WHAT RUNS HERE
#   1. AMFI schemes + NAV       (published ~11pm IST on business days)
#   2. NAV split detection      (before anything reads NAV as a series)
#   3. Stock candles + technicals   (market days only)
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
#   Kite has no market calendar to consult, so asking for Saturday's
#   candles means 2,150 requests that each correctly return nothing.
#   Weekday holidays still cost that, because those we cannot know
#   without carrying an exchange calendar -- one wasted run a few times
#   a year is a fair price for not maintaining one.
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

# ---------------------------------------------------------------------
# THE STOCK SIDE.
#
# Ordering is not cosmetic. score_stocks.py reads stock_technical, and
# score_funds.py reads stock_score -- so these two must finish BEFORE
# fund scores, or the fund scores are computed against yesterday's view
# of the market and still report success.
# ---------------------------------------------------------------------
# WHY THE BATCH BRAKE IS RE-SIZED HERE.
#   The defaults inside fetch_technicals.py (50 stocks, then a 30-second
#   pause) were set when every stock meant three large multi-year
#   downloads. That is 45 pauses over the universe -- 22 minutes of
#   waiting, MORE than the fetching itself now takes.
#
#   With candles stored, each stock is one small request for one day.
#   The rate limiter -- deliberately 2.5/sec against Kite's allowed 3 --
#   is what actually keeps us inside the limit; the batch pause is a
#   second, coarser courtesy. Sized for the new shape of the run it is
#   11 pauses of 10 seconds, under two minutes, and the whole step lands
#   around 20 minutes instead of 40.
#
#   The defaults in the script are left alone on purpose: a first run,
#   or a --rebuild-ohlc, still downloads full history and still wants
#   the gentler brake.
if [ "$DOW" -le 5 ]; then
    run_step "Stock candles and technicals" \
             $PY fetch_technicals.py --fresh --batch-size 200 --batch-pause 10
    run_step "Stock scores"          $PY score_stocks.py
else
    skip_step "Stock candles and technicals" "weekend, market was shut"
    skip_step "Stock scores"                 "weekend, market was shut"
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
