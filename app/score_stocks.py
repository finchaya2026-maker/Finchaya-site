"""
score_stocks.py
---------------
Computes a technical score per stock from stock_technical, writes stock_score.

TIMEFRAME SELECTION (your spec)
    MONTHLY if available, else WEEKLY, else DAILY.
    "Available" means at least 2 consecutive bars, because half the rules
    compare this candle to the previous one.

    Set BLEND_WEIGHTS below to score all three and combine instead.

THE RULES, as specified
    ADX     GATED ON DIRECTION. ADX measures how strong a trend is and says
            nothing about which way it runs, so the same reading is read
            against +DI/-DI to find out. Ungated, a stock in a hardening
            DOWNTREND collected the full +8 -- Infosys did exactly that in
            August 2026 while every other indicator on it read bearish.

                                       +DI > -DI      -DI > +DI
              rising,  adx <  55          +8             -4
              rising,  adx >= 55          +1             -1
              falling, adx >= 55          -1             +1
              falling, adx <  55           0              0
              +DI/-DI missing              0              0

            The two halves mirror rather than match. Rising below 55 pays +8
            up and charges -4 down, because a fund can only be long: a
            building uptrend is actionable, a building downtrend is mostly a
            reason not to own it. The >=55 rows discount BOTH directions on
            the same exhaustion logic -- a trend that extended has less left
            to pay for, whichever way it points. The falling rows invert
            because a weakening downtrend is mildly good news.
    MACD    line > 0  : base +2, rising +3, above signal +3  (max 8)
            line <= 0 :          rising +2, above signal +2  (max 4)
    RSI     > 50                   -> +4
            40 to 50               -> +1
            < 40                   -> -1
    BB      upper rising AND close > bb_middle   -> +4
            lower falling AND close < bb_middle  -> -2
            otherwise                            ->  0
    ST      bullish and rising     -> +8
            bullish                -> +4
            bearish and falling    -> -1
            bearish                -> +1

    Range: -8 (worst) to +32 (best). The floor moved from -5 because the
    ADX component can now reach -4 where it previously bottomed at -1.

TIE-BREAKS -- not in your spec, so stated here rather than buried:
  * "rising" is STRICT. An exactly-flat ADX counts as falling. On float data
    an exact tie is vanishingly rare, but it must resolve somewhere.
  * ADX exactly 55 counts as the >= 55 bucket.
  * RSI exactly 50 scores 1 (since "> 50" is strict); exactly 40 scores 1.
  * Supertrend "rising" means the supertrend LINE rose. While bullish that
    line is the lower band ratcheting up = trend strengthening. While bearish
    it is the upper band ratcheting down, so "falling" = downtrend
    strengthening, which is why bearish+falling scores worse than bearish.

USAGE
    python score_stocks.py                      # score latest available date
    python score_stocks.py --date 2026-08-25
    python score_stocks.py --explain RELIANCE   # show the full working
"""

import argparse
import os
import sys
from datetime import date

import psycopg
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("FINCHAYA_DB",
               "host=localhost port=5432 dbname=finchaya "
               "user=postgres password=Finchaya@2026")

ALGO_VERSION = "stock-v3"
# The ungated ADX rule this replaces. Kept runnable under --v2 so the old
# series can be regenerated for comparison. NOT a fallback: nothing should
# read stock-v2 once v3 is live.
UNGATED_ALGO = "stock-v2"
# The earlier engine's rules, written under their OWN algo_version so the two
# never overwrite each other. stock_score is keyed on
# (isin, as_of_date, algo_version), so this needs no schema change and cannot
# disturb anything already scored.
LEGACY_ALGO = "stock-v2l"
# MACD LINE ONLY. One rule, no ADX, RSI, Bollinger or supertrend:
#   macd_line > previous candle's macd_line -> 4, otherwise 0.
# The point of it is subtraction. If a five-indicator score and a one-rule
# score come out the same, the other four indicators were never doing work.
MACD_ALGO = "stock-v2m"

# Timeframe preference, most preferred first.
TIMEFRAME_PRIORITY = ["MONTHLY", "WEEKLY", "DAILY"]

# Set to e.g. {"MONTHLY": 0.5, "WEEKLY": 0.3, "DAILY": 0.2} to blend all three
# instead of falling back. None = use the priority list above.
BLEND_WEIGHTS = None

MAX_SCORE, MIN_SCORE = 32, -8
# The ungated rule floors ADX at -1, so a total built on it stops at -5.
MAX_SCORE_V2, MIN_SCORE_V2 = 32, -5
# The old ADX table floors at -2 rather than -1, so a total built on it runs
# one point lower.
MAX_SCORE_OLD, MIN_SCORE_OLD = 32, -6
MAX_SCORE_MACD, MIN_SCORE_MACD = 4, 0


# =====================================================================
# THE RULES
# =====================================================================
# The four payouts, named so a change is one number rather than a hunt
# through branches. Sign conventions live in the docstring table above.
ADX_BUILD_UP     =  8.0   # rising below 55, buyers in control
ADX_BUILD_DOWN   = -4.0   # rising below 55, sellers in control
ADX_STRETCH_UP   =  1.0   # rising at/above 55 -- little left to pay for
ADX_STRETCH_DOWN = -1.0
ADX_FADE_UP      = -1.0   # falling from a big reading: uptrend coming apart
ADX_FADE_DOWN    =  1.0   # ... or a downtrend coming apart, which helps
ADX_QUIET        =  0.0   # falling below the threshold: no strong opinion


def score_adx(adx_now, adx_prev, plus_di, minus_di):
    """ADX gated on the directional lines.

    WHY THE GATE EXISTS. ADX is a magnitude with no sign. Rising means the
    trend is hardening; it does not mean the trend is up. The ungated rule
    paid +8 for that hardening either way, so a stock falling with
    conviction scored like a stock rising with conviction. In a falling
    market that is not a rounding error -- it is most of the universe.

    A MISSING DI PAIR SCORES ZERO, NOT +8. The old behaviour would have
    been to keep paying out where direction is unknowable, which preserves
    the bug precisely on the rows nobody can check. Coverage is currently
    100% on all three timeframes, so this branch should stay unused; it is
    here so that stops being an assumption.
    """
    if adx_now is None or adx_prev is None:
        return 0.0
    if plus_di is None or minus_di is None:
        return 0.0

    up = plus_di > minus_di              # strict: an exact tie reads as down
    rising = adx_now > adx_prev          # strict: flat counts as falling
    high = adx_now >= 55

    if rising:
        if high:
            return ADX_STRETCH_UP if up else ADX_STRETCH_DOWN
        return ADX_BUILD_UP if up else ADX_BUILD_DOWN
    if high:
        return ADX_FADE_UP if up else ADX_FADE_DOWN
    return ADX_QUIET


def score_adx_ungated(adx_now, adx_prev):
    """The pre-v3 rule, direction-blind. Retained for --v2 only."""
    if adx_now is None or adx_prev is None:
        return 0.0
    rising = adx_now > adx_prev
    high = adx_now >= 55
    if rising:
        return 1.0 if high else 8.0
    return -1.0 if high else 0.0


def score_adx_legacy(adx_now, adx_prev):
    """The earlier engine's ADX table.

        rising, ADX >= 55   ->  0   already stretched; nothing left to pay for
        rising, 15 <= ADX   -> +8   the trend is BUILDING -- the only payout
        rising, ADX <  15   ->  0   no trend yet, so no opinion
        falling, ADX >= 50  -> -2   a large trend actively coming apart
        falling, ADX <  50  -> -1   conviction draining out of a lesser one

    Two differences from the rule above do the work, and they pull the same
    way. The current rule pays a full 8 to a directionless stock at ADX 6
    ticking up to 7; this pays nothing below 15. The current rule is neutral on
    a weakening trend below the threshold; this charges -1.

    So this version refuses to pay for strength already banked and charges for
    strength draining away, where the current one rewards any uptick. On a
    momentum score that matters: paying more the stronger a trend gets is
    buying tops, which is what mean reversion then takes back.

    The pivots are asymmetric on purpose -- 55 rising, 50 falling -- so ADX 52
    is worth +8 one way and -2 the other. A ten-point swing on one bar's
    wobble, sitting where trending readings cluster. As specified in the
    original engine, not a transcription slip.
    """
    if adx_now is None or adx_prev is None:
        return 0.0
    if adx_now > adx_prev:
        if adx_now >= 55:
            return 0.0
        return 8.0 if adx_now >= 15 else 0.0
    return -2.0 if adx_now >= 50 else -1.0


def score_macd(line_now, line_prev, signal_now):
    """The MACD zero line sets the REGIME; momentum is then weighted by it.

        line > 0 : base +2, rising +3, above signal +3   -> max 8
        line <= 0:          rising +2, above signal +2   -> max 4

    The +2 base matters: without it, a positive-but-cooling MACD scores the
    same as a negative-and-cooling one, which throws away the only thing the
    zero line tells you -- which regime the stock is in, regardless of what
    it did this month.

    Exactly 0 falls in the negative branch, since "> 0" is strict.
    """
    if line_now is None:
        return 0.0

    above_zero = line_now > 0
    rising = line_prev is not None and line_now > line_prev
    above_signal = signal_now is not None and line_now > signal_now

    if above_zero:
        points = 2.0
        if rising:
            points += 3.0
        if above_signal:
            points += 3.0
    else:
        points = 0.0
        if rising:
            points += 2.0
        if above_signal:
            points += 2.0
    return points


def score_rsi(rsi):
    if rsi is None:
        return 0.0
    if rsi > 50:
        return 4.0
    if rsi >= 40:
        return 1.0
    return -1.0


def score_bb(upper_now, upper_prev, lower_now, lower_prev, close, middle):
    """bb_middle IS the 20-period SMA the bands are built on, which is the
    'sma(20)' your rule refers to."""
    if None in (upper_now, upper_prev, lower_now, lower_prev, close, middle):
        return 0.0
    if upper_now > upper_prev and close > middle:
        return 4.0
    if lower_now < lower_prev and close < middle:
        return -2.0
    return 0.0


def score_supertrend(direction, st_now, st_prev):
    if direction is None:
        return 0.0
    bullish = direction == "UP"
    moved = None
    if st_now is not None and st_prev is not None:
        moved = "RISING" if st_now > st_prev else "FALLING"

    if bullish:
        return 8.0 if moved == "RISING" else 4.0
    return -1.0 if moved == "FALLING" else 1.0


def score_bar(cur, prev, legacy=False, macd_only=False, ungated=False):
    """cur/prev are dicts of one bar's indicator values.

    legacy=True and ungated=True each swap ONLY the ADX rule. Every other
    component is identical, so any difference between the engines is that
    rule and nothing else.
    """
    if macd_only:
        # Deliberately not routed through score_macd: that rule also reads the
        # zero line and the signal. This is the raw direction of the MACD line
        # and nothing else. A missing previous value scores 0, same as a fall.
        up = (cur["macd_line"] is not None and prev["macd_line"] is not None
              and cur["macd_line"] > prev["macd_line"])
        return {"adx_score": 0.0, "macd_score": 4.0 if up else 0.0,
                "rsi_score": 0.0, "bb_score": 0.0, "supertrend_score": 0.0,
                "total_score": 4.0 if up else 0.0}

    components = {
        "adx_score": (score_adx_legacy(cur["adx"], prev["adx"]) if legacy
                      else score_adx_ungated(cur["adx"], prev["adx"]) if ungated
                      else score_adx(cur["adx"], prev["adx"],
                                     cur["plus_di"], cur["minus_di"])),
        "macd_score": score_macd(cur["macd_line"], prev["macd_line"],
                                 cur["macd_signal"]),
        "rsi_score": score_rsi(cur["rsi"]),
        "bb_score": score_bb(cur["bb_upper"], prev["bb_upper"],
                             cur["bb_lower"], prev["bb_lower"],
                             cur["close_price"], cur["bb_middle"]),
        "supertrend_score": score_supertrend(cur["supertrend_dir"],
                                             cur["supertrend_value"],
                                             prev["supertrend_value"]),
    }
    components["total_score"] = sum(components.values())
    return components


# =====================================================================
# DATA ACCESS
# =====================================================================
# WHY THIS IS MORE COMPLICATED THAN "TAKE THE LAST TWO ROWS":
#
# Weekly and monthly bars are labelled with the last TRADING DAY inside
# their period, and that label advances every day the period is still
# open. Run the fetch on the 25th and again on the 26th and August's
# monthly candle is stored twice -- as 2026-08-25 and 2026-08-26.
#
# Naively taking the two newest rows then compares August to August:
# ADX identical, Supertrend identical, every previous-candle rule dead.
#
# So: collapse to one row per CALENDAR period (keeping the freshest
# observation of each), then take the two most recent periods.
FETCH = """
WITH bars AS (
    SELECT t.*,
           CASE timeframe
               WHEN 'MONTHLY' THEN DATE_TRUNC('month', as_of_date)::date
               WHEN 'WEEKLY'  THEN DATE_TRUNC('week',  as_of_date)::date
               ELSE as_of_date
           END AS period
    FROM stock_technical t
    WHERE as_of_date <= %s
),
one_per_period AS (
    SELECT DISTINCT ON (isin, timeframe, period) *
    FROM bars
    ORDER BY isin, timeframe, period, as_of_date DESC
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY isin, timeframe
                              ORDER BY period DESC) AS rn
    FROM one_per_period
)
SELECT isin, timeframe, as_of_date, period, rn,
       adx, plus_di, minus_di,
       rsi, macd_line, macd_signal, macd_histogram,
       bb_upper, bb_middle, bb_lower, sma,
       supertrend_value, supertrend_dir, close_price, param_set
FROM ranked
WHERE rn <= 2
ORDER BY isin, timeframe, rn
"""


def load_bars(conn, as_of):
    """-> {isin: {timeframe: [current_bar, previous_bar]}}"""
    with conn.cursor() as cur:
        cur.execute(FETCH, (as_of,))
        cols = [d[0] for d in cur.description]
        data = {}
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            r = {k: (float(v) if hasattr(v, "quantize") else v)
                 for k, v in r.items()}
            data.setdefault(r["isin"], {}).setdefault(r["timeframe"], []).append(r)
    return data


def pick_timeframe(by_timeframe):
    """Your fallback: monthly, else weekly, else daily.
    A timeframe only counts as available with 2 bars -- half the rules
    need a previous candle, and scoring one without it silently zeroes
    three of the five components."""
    for timeframe in TIMEFRAME_PRIORITY:
        bars = by_timeframe.get(timeframe)
        if bars and len(bars) >= 2:
            return timeframe, bars
    return None, None


UPSERT = """
INSERT INTO stock_score
    (isin, as_of_date, timeframe_used, total_score,
     adx_score, macd_score, rsi_score, bb_score, supertrend_score,
     adx, rsi, macd_line, supertrend_dir, close_price,
     algo_version, param_set, run_id)
VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s)
ON CONFLICT (isin, as_of_date, algo_version) DO UPDATE SET
    timeframe_used=EXCLUDED.timeframe_used,
    total_score=EXCLUDED.total_score,
    adx_score=EXCLUDED.adx_score, macd_score=EXCLUDED.macd_score,
    rsi_score=EXCLUDED.rsi_score, bb_score=EXCLUDED.bb_score,
    supertrend_score=EXCLUDED.supertrend_score,
    adx=EXCLUDED.adx, rsi=EXCLUDED.rsi, macd_line=EXCLUDED.macd_line,
    supertrend_dir=EXCLUDED.supertrend_dir, close_price=EXCLUDED.close_price,
    param_set=EXCLUDED.param_set, run_id=EXCLUDED.run_id,
    created_at=NOW()
"""


# =====================================================================
def explain(conn, symbol, as_of):
    with conn.cursor() as cur:
        cur.execute("SELECT isin FROM stock_master WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
    if not row:
        print(f"{symbol}: not in stock_master")
        return
    isin = row[0]

    data = load_bars(conn, as_of)
    by_tf = data.get(isin)
    if not by_tf:
        print(f"{symbol}: no technical data")
        return

    print(f"\n{symbol}  ({isin})")
    for timeframe in TIMEFRAME_PRIORITY:
        bars = by_tf.get(timeframe, [])
        print(f"  {timeframe:<8} {len(bars)} bar(s) available"
              + ("" if len(bars) >= 2 else "   <- unusable, needs 2"))

    timeframe, bars = pick_timeframe(by_tf)
    if not timeframe:
        print("  -> cannot score: no timeframe has 2 bars")
        return

    cur_bar, prev_bar = bars[0], bars[1]
    print(f"\n  Using {timeframe}: {cur_bar['as_of_date']} "
          f"(period {cur_bar['period']}) vs {prev_bar['as_of_date']} "
          f"(period {prev_bar['period']})\n")

    s = score_bar(cur_bar, prev_bar)

    def arrow(now, before):
        if now is None or before is None:
            return "?"
        return "UP" if now > before else "DOWN"

    di = ("DI n/a" if cur_bar["plus_di"] is None or cur_bar["minus_di"] is None
          else f"+DI {cur_bar['plus_di']:.1f} / -DI {cur_bar['minus_di']:.1f} "
               f"({'UP' if cur_bar['plus_di'] > cur_bar['minus_di'] else 'DOWN'})")
    print(f"  ADX   {cur_bar['adx']:.2f} (prev {prev_bar['adx']:.2f}, "
          f"{arrow(cur_bar['adx'], prev_bar['adx'])})  {di}"
          f"  -> {s['adx_score']:+.0f}")
    print(f"  MACD  line {cur_bar['macd_line']:.3f}, prev {prev_bar['macd_line']:.3f}, "
          f"signal {cur_bar['macd_signal']:.3f}  -> {s['macd_score']:+.0f}")
    print(f"  RSI   {cur_bar['rsi']:.2f}{'':>28}-> {s['rsi_score']:+.0f}")
    print(f"  BB    upper {cur_bar['bb_upper']:.2f} (prev {prev_bar['bb_upper']:.2f}), "
          f"close {cur_bar['close_price']:.2f} vs mid {cur_bar['bb_middle']:.2f} "
          f"-> {s['bb_score']:+.0f}")
    print(f"  ST    {cur_bar['supertrend_dir']}, line {cur_bar['supertrend_value']:.2f} "
          f"(prev {prev_bar['supertrend_value']:.2f}, "
          f"{arrow(cur_bar['supertrend_value'], prev_bar['supertrend_value'])})"
          f" -> {s['supertrend_score']:+.0f}")
    print(f"  {'-' * 56}")
    print(f"  TOTAL {s['total_score']:+.0f}  (range {MIN_SCORE} to {MAX_SCORE})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="score as at this date (default: latest)")
    ap.add_argument("--explain", help="show the working for one symbol")
    ap.add_argument("--macd-only", action="store_true", dest="macd_only",
                    help="MACD line direction alone, 4 or 0. Writes "
                         "algo_version stock-v2m; touches nothing else.")
    ap.add_argument("--v2", action="store_true", dest="ungated",
                    help="score with the OLD direction-blind ADX rule, "
                         "written under algo_version stock-v2. For "
                         "regenerating the pre-gate series to compare "
                         "against; not for production use.")
    ap.add_argument("--legacy", action="store_true",
                    help="score with the earlier ADX rule, written under "
                         "algo_version stock-v2l (does not touch stock-v2)")
    args = ap.parse_args()

    with psycopg.connect(DB) as conn:
        if args.date:
            as_of = date.fromisoformat(args.date)
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(as_of_date) FROM stock_technical")
                as_of = cur.fetchone()[0]
            if not as_of:
                print("stock_technical is empty.")
                sys.exit(1)

        if args.explain:
            explain(conn, args.explain.upper(), as_of)
            return

        algo = (MACD_ALGO if args.macd_only
                else LEGACY_ALGO if args.legacy
                else UNGATED_ALGO if args.ungated else ALGO_VERSION)
        lo, hi = ((MIN_SCORE_MACD, MAX_SCORE_MACD) if args.macd_only
                  else (MIN_SCORE_OLD, MAX_SCORE_OLD) if args.legacy
                  else (MIN_SCORE_V2, MAX_SCORE_V2) if args.ungated
                  else (MIN_SCORE, MAX_SCORE))
        print(f"Scoring as at {as_of} ({algo}, range {lo} to {hi})")

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO batch_run_log (run_type, business_date, status)
                VALUES ('SCORE_STOCKS', %s, 'RUNNING') RETURNING run_id
            """, (as_of,))
            run_id = cur.fetchone()[0]
            conn.commit()

        data = load_bars(conn, as_of)
        print(f"Loaded technicals for {len(data)} stocks")

        rows = []
        used = {"MONTHLY": 0, "WEEKLY": 0, "DAILY": 0}
        unscoreable = []

        for isin, by_tf in data.items():
            timeframe, bars = pick_timeframe(by_tf)
            if not timeframe:
                available = {k: len(v) for k, v in by_tf.items()}
                unscoreable.append((isin, available))
                continue

            cur_bar, prev_bar = bars[0], bars[1]
            s = score_bar(cur_bar, prev_bar, legacy=args.legacy,
                          macd_only=args.macd_only, ungated=args.ungated)
            used[timeframe] += 1

            rows.append((
                isin, as_of, timeframe, s["total_score"],
                s["adx_score"], s["macd_score"], s["rsi_score"],
                s["bb_score"], s["supertrend_score"],
                cur_bar["adx"], cur_bar["rsi"], cur_bar["macd_line"],
                cur_bar["supertrend_dir"], cur_bar["close_price"],
                algo, cur_bar["param_set"], run_id,
            ))

        if not rows:
            print("\nNothing scoreable. Most likely cause: technicals were")
            print("fetched with --history 1, so there is no previous candle.")
            print("Re-run: python fetch_technicals.py --history 3")
            with conn.cursor() as cur:
                cur.execute("""UPDATE batch_run_log SET status='FAILED',
                               finished_at=NOW(), error_message='no scoreable stocks'
                               WHERE run_id=%s""", (run_id,))
                conn.commit()
            sys.exit(1)

        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
            cur.execute("""
                UPDATE batch_run_log SET status='SUCCESS', finished_at=NOW(),
                       rows_read=%s, rows_written=%s WHERE run_id=%s
            """, (len(data), len(rows), run_id))
        conn.commit()

        # ---------------- report ----------------
        print(f"\nScored {len(rows)} stocks. run_id = {run_id}")
        print("\nTimeframe used:")
        for tf, n in used.items():
            print(f"  {tf:<9} {n:>5}")
        if unscoreable:
            print(f"\n{len(unscoreable)} unscoreable (need 2 bars in some timeframe)")
            for isin, avail in unscoreable[:5]:
                print(f"  {isin}  {avail}")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(total_score),2), MIN(total_score), MAX(total_score),
                       COUNT(*) FILTER (WHERE total_score >= 21),
                       COUNT(*) FILTER (WHERE total_score <= 4)
                FROM stock_score WHERE as_of_date=%s AND algo_version=%s
            """, (as_of, algo))
            avg, lo, hi, strong, weak = cur.fetchone()
            print(f"\nScore distribution: avg {avg}, range {lo} to {hi}")
            print(f"  strong (>=21): {strong}   weak (<=4): {weak}")

            cur.execute("""
                SELECT m.symbol, s.total_score, s.timeframe_used,
                       s.adx_score, s.macd_score, s.rsi_score,
                       s.bb_score, s.supertrend_score
                FROM stock_score s JOIN stock_master m USING (isin)
                WHERE s.as_of_date=%s AND s.algo_version=%s
                ORDER BY s.total_score DESC LIMIT 10
            """, (as_of, algo))
            print("\nTop 10 (symbol, total, tf, adx, macd, rsi, bb, st):")
            for r in cur.fetchall():
                print("  ", r)


if __name__ == "__main__":
    main()
