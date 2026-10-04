"""
create_market_view.py -- the tactical tilt layer.
--------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_market_view.py

WHAT THIS IS
    A view is a dated, signed set of tilts in percentage points, applied on
    top of the allocation rules. It never edits them.

    final = clamp(base_target + tilt, floor, ceiling)

WHY IT IS A SEPARATE LAYER AND NOT AN EDIT TO THE RULES
    Three reasons, and the first is the one that matters most.

    You can score yourself. With tilts stored as dated rows you can ask, a
    year later, what happened after going overweight mid cap in March.
    Fold the view into the base rule and that question is unanswerable
    for ever -- the evidence of what you thought and when is gone.

    Views expire; rules do not. A bullish tilt written into a rule becomes
    permanent silently, and is still there in the next bear market. As its
    own row with a review date it lapses back to neutral instead.

    And it is explainable. "The rule says 31% mid cap, I added 8 points on
    this view, dated, and here is why" is a different conversation from a
    number that simply changed.

THE SUM-TO-ZERO CONSTRAINT
    Tilts in one view must add to zero. "More mid cap" is not a view --
    "more mid cap, funded out of large cap" is. Forcing the second names
    what is being given up, and keeps the allocation at 100 without any
    special handling downstream.

    It is enforced when a view is ACTIVATED rather than on each row, since
    a set under construction is legitimately unbalanced.
"""

import os
import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

DDL = """
CREATE TABLE IF NOT EXISTS market_view (
    view_id        bigserial PRIMARY KEY,
    label          text NOT NULL,
    rationale      text,

    -- A view is DRAFT while being written, ACTIVE once it applies, and
    -- RETIRED once it does not. Retired rows are kept: they are the record
    -- of what was thought and when, which is the whole point of the layer.
    status         text NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','active','retired')),

    effective_from date,
    -- When to look at this again. A view with no review date is a view
    -- that quietly becomes permanent.
    review_by      date,
    retired_at     date,

    author_user_id bigint,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_view_tilt (
    view_id    bigint NOT NULL REFERENCES market_view(view_id) ON DELETE CASCADE,
    bucket     text   NOT NULL CHECK (bucket IN
                 ('large','mid','small','debt','gold','international')),
    -- Percentage POINTS, signed. +8 on mid means eight points more than
    -- the rule says, and something else must give up eight.
    tilt_pct   numeric NOT NULL CHECK (tilt_pct BETWEEN -40 AND 40),
    PRIMARY KEY (view_id, bucket)
);

-- At most one active view at a time. Two active views would have to be
-- added together, and the sum of two balanced sets is only balanced by
-- luck -- so the constraint is one, enforced here rather than hoped for.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_view
    ON market_view ((status)) WHERE status = 'active';
"""

with psycopg.connect(os.getenv("FINCHAYA_DB")) as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
    for t in ("market_view", "market_view_tilt"):
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name = %s ORDER BY ordinal_position""", (t,))
        print("%-18s %s" % (t, ", ".join(r[0] for r in cur.fetchall())))
    cur.execute("SELECT COUNT(*) FROM market_view WHERE status = 'active'")
    print("active views:", cur.fetchone()[0], "(none is the neutral state)")
