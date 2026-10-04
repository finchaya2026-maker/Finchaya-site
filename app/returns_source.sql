-- ============================================================
-- returns_source.sql
-- Run once in pgAdmin against DROPLET - finchaya_mf.
--
-- THE PROBLEM
-- An IDCW scheme's NAV falls by every distribution it pays. JM Large Cap
-- Regular IDCW dropped from 19.96 to 11.78 on one day in March 2020 --
-- a payout, not a loss, and its eleven sibling variants all rose that
-- day. A trailing return computed on that NAV misses everything paid
-- out, and we then compare it against a TOTAL return index. The fund
-- loses on both sides of a comparison it never had a fair shot at.
--
-- THE FIX
-- Returns are computed from the Growth sibling of the same fund and the
-- same plan, where one exists. Growth NAV retains distributions, so it
-- measures what the manager actually achieved -- which is the question
-- a reader is asking when they look up a fund.
--
-- The substitution is recorded, not hidden: mf_returns.nav_scheme_code
-- says which series produced the number.
-- ============================================================

ALTER TABLE mf_returns
    ADD COLUMN IF NOT EXISTS nav_scheme_code varchar(20);

COMMENT ON COLUMN mf_returns.nav_scheme_code IS
  'Which scheme''s NAV produced this return. Differs from scheme_code '
  'when an IDCW scheme borrowed its Growth sibling''s series.';


CREATE OR REPLACE VIEW v_returns_source AS
SELECT
    s.scheme_code,
    COALESCE(g.scheme_code, s.scheme_code) AS nav_scheme_code,
    (g.scheme_code IS NOT NULL)            AS substituted
FROM mf_scheme s
LEFT JOIN LATERAL (
    -- Same fund, same plan, Growth option. Variants of one fund share a
    -- scheme_name exactly, so no fuzzy matching is needed or wanted --
    -- a loose match here would borrow another fund's returns.
    SELECT g.scheme_code
    FROM mf_scheme g
    WHERE g.option_type = 'GROWTH'
      AND g.scheme_name = s.scheme_name
      AND COALESCE(g.plan_type, '') = COALESCE(s.plan_type, '')
      AND g.scheme_code <> s.scheme_code
    ORDER BY
      -- prefer a sibling that actually has NAV history
      EXISTS (SELECT 1 FROM mf_nav n WHERE n.scheme_code = g.scheme_code) DESC,
      g.scheme_code
    LIMIT 1
) g ON s.option_type = 'IDCW';


-- How many funds this changes, and how many IDCW schemes have no Growth
-- sibling to borrow from (those keep their own, understated, series).
SELECT
    COUNT(*) FILTER (WHERE substituted)            AS using_growth_sibling,
    COUNT(*) FILTER (WHERE NOT substituted
                       AND m.option_type = 'IDCW') AS idcw_with_no_sibling,
    COUNT(*)                                       AS scored_funds
FROM mf_score sc
JOIN v_returns_source v USING (scheme_code)
JOIN mf_scheme m ON m.scheme_code = sc.scheme_code
WHERE sc.as_of_date = (SELECT MAX(as_of_date) FROM mf_score);
