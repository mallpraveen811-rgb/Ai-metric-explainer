-- =====================================================================
-- 02_monthly_metrics.sql  |  The 4 KPIs per month, their change, and how unusual it is
-- Runs inside the schema given by -v schema=... (default: insight), so the same
-- file is reused by the planted-anomaly test on a modified copy of the data.
--
-- Metrics:   revenue (R$), orders, late_pct (% of delivered orders that were late),
--            avg_review_score (1-5)
-- z_score:   (this month's change - average of the previous 6 changes)
--            / standard deviation of those 6 changes.
--            |z| >= 2 means "this change is unusual for this metric".
--            Only computed once at least 4 earlier changes exist (from mid-2017).
-- =====================================================================
\if :{?schema}
\else
  \set schema insight
\endif
SET search_path = :"schema", public;

DROP VIEW IF EXISTS monthly_metrics CASCADE;

CREATE VIEW monthly_metrics AS
WITH m AS (
  SELECT order_month,
         count(*)                         AS orders,
         sum(order_value)                 AS revenue,
         count(is_late)                   AS delivered,
         count(*) FILTER (WHERE is_late)  AS late,
         count(review_score)              AS reviewed,
         sum(review_score)                AS review_sum
  FROM order_facts
  GROUP BY order_month
), long AS (                                   -- one row per metric per month
  SELECT order_month, 'revenue' AS metric, revenue::numeric AS value FROM m
  UNION ALL SELECT order_month, 'orders', orders::numeric FROM m
  UNION ALL SELECT order_month, 'late_pct', 100.0 * late / NULLIF(delivered, 0) FROM m
  UNION ALL SELECT order_month, 'avg_review_score', review_sum::numeric / NULLIF(reviewed, 0) FROM m
), ch AS (
  SELECT metric, order_month, value,
         lag(value) OVER w           AS prev_value,
         value - lag(value) OVER w   AS change
  FROM long
  WINDOW w AS (PARTITION BY metric ORDER BY order_month)
)
SELECT metric, order_month,
       round(value, 4)                                             AS value,
       round(prev_value, 4)                                        AS prev_value,
       round(change, 4)                                            AS change,
       round(100 * change / NULLIF(prev_value, 0), 2)              AS change_pct,
       CASE WHEN count(change) OVER t >= 4            -- need at least 4 earlier changes to judge
            THEN round((change - avg(change) OVER t) / NULLIF(stddev_samp(change) OVER t, 0), 2) END AS z_score,
       count(change) OVER t                                        AS history_months
FROM ch
WINDOW t AS (PARTITION BY metric ORDER BY order_month ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING);
