"""
create_alert_rules.py -- where a portfolio's alert choices are kept.
--------------------------------------------------------------------
Run once. Safe to re-run: every statement is IF NOT EXISTS.

    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_alert_rules.py

WHAT THIS IS AND IS NOT
    It stores what a person has ASKED to be told about. It does not send
    anything, and nothing in the product sends anything yet. The screen
    that writes these rows says so in as many words, because a switch
    labelled "alert me" that silently does nothing is worse than no
    switch: it converts a person's attention into a false sense of cover.

WHY THE RULES ARE ROWS AND NOT A JSONB BLOB
    Because the thing that will eventually read them is a nightly job that
    asks "which portfolios want to hear about overlap crossing 50%?" --
    one indexed read across every portfolio. A blob would mean loading
    every portfolio's settings and filtering in Python, every night,
    forever. Rows also let a threshold be a number the database can
    compare, rather than a string somebody has to remember to cast.

WHY THE THRESHOLD LIVES WITH THE RULE
    "Tell me when a fund's rising score falls by more than 20%" is one
    decision, not two. Keeping the number beside the switch means the
    evaluator never has to guess a default for somebody who set one, and
    a default that changes later cannot silently rewrite what a person
    chose.

WHY THERE IS A CHANNEL TABLE AT ALL, GIVEN NOTHING SENDS
    Because "which portfolios want email" is a different question from
    "which rules are on", and folding the two together would mean either a
    channel column repeated on every rule row or a second migration the
    day delivery is built. One small table now costs nothing.

    Contact details are deliberately NOT stored here. The account already
    knows the person's email; a phone number for WhatsApp is personal data
    with its own consent question, and that question belongs to the day
    delivery is actually built, not to a screen that cannot send anything.
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

DDL = """
-- One row per rule a portfolio has an opinion about. A rule nobody has
-- touched has no row at all, so the catalogue's default applies and the
-- table stays small.
CREATE TABLE IF NOT EXISTS portfolio_alert (
    portfolio_id  int         NOT NULL
                  REFERENCES portfolio(portfolio_id) ON DELETE CASCADE,
    rule_key      text        NOT NULL,
    enabled       boolean     NOT NULL DEFAULT false,
    -- NULL means "use the catalogue default". Rules with nothing to tune
    -- (a fund stopped disclosing) leave it NULL forever.
    threshold     numeric,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, rule_key)
);

-- The nightly evaluator's question is "who wants this rule?", not "what
-- does this portfolio want?", so the index runs rule-first.
CREATE INDEX IF NOT EXISTS portfolio_alert_rule_on
    ON portfolio_alert (rule_key) WHERE enabled;

CREATE TABLE IF NOT EXISTS portfolio_alert_channel (
    portfolio_id  int         NOT NULL
                  REFERENCES portfolio(portfolio_id) ON DELETE CASCADE,
    channel       text        NOT NULL,     -- email | whatsapp | push
    enabled       boolean     NOT NULL DEFAULT false,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, channel)
);
"""

# mfapp is the role the API connects as; doadmin owns the tables.
GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON portfolio_alert TO mfapp;
GRANT SELECT, INSERT, UPDATE, DELETE ON portfolio_alert_channel TO mfapp;
"""


def main():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        try:
            cur.execute(GRANTS)
        except psycopg.errors.UndefinedObject:
            # A local database without the mfapp role. Not a failure --
            # the tables are what this script is for.
            conn.rollback()
            cur.execute(DDL)
            print("note: role mfapp does not exist here, grants skipped.")
        conn.commit()

        cur.execute("""
            SELECT table_name,
                   (SELECT count(*) FROM information_schema.columns c
                     WHERE c.table_name = t.table_name) AS cols
            FROM information_schema.tables t
            WHERE table_name IN ('portfolio_alert', 'portfolio_alert_channel')
            ORDER BY table_name
        """)
        for r in cur.fetchall():
            print("ready: %s (%d columns)" % (r[0], r[1]))

    print()
    print("Nothing sends alerts yet. These tables record what people have")
    print("asked for, so that when delivery is built it has something to")
    print("deliver -- and so the choices made today are not lost.")


if __name__ == "__main__":
    main()
