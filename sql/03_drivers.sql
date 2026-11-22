-- =====================================================================
-- 03_drivers.sql  |  WHY did a metric move? Split every monthly change by segment.
--
-- For each month vs the month before, and for 6 dimensions (customer region,
-- customer state, product category, seller state, payment type, new/returning
-- customer), each segment gets a CONTRIBUTION. Contributions of all segments in a
-- dimension add up EXACTLY to the total change, so "SP explains 60% of the drop"
-- is arithmetic, not a guess.
--
--   revenue           contribution = segment revenue now - segment revenue before
--                     split into volume effect (more/fewer orders, at the old
--                     average order value) + value effect (bigger/smaller orders)
--   orders            contribution = segment orders now - before
--   late_pct          contribution = (segment late orders / ALL delivered orders) now
--                                  - the same share before            [percentage points]
--   avg_review_score  contribution = (segment score total / ALL reviews) now
--                                  - the same before                  [score points]
--   The two rate formulas are exact because the total rate is the sum of those
--   shares (e.g. late% = sum over segments of segment_late / all_delivered).
-- =====================================================================
\if :{?schema}
\else
  \set schema insight
\endif
SET search_path = :"schema", public;

DROP VIEW IF EXISTS segment_month, drivers CASCADE;

-- every order counted once per dimension (LATERAL VALUES = an "unpivot")
CREATE VIEW segment_month AS
SELECT o.order_month, d.dimension, d.segment,
       count(*)                         AS orders,
       sum(o.order_value)               AS revenue,
       count(o.is_late)                 AS delivered,
       count(*) FILTER (WHERE o.is_late) AS late,
       count(o.review_score)            AS reviewed,
       sum(o.review_score)              AS review_sum
FROM order_facts o
CROSS JOIN LATERAL (VALUES
    ('Customer region',  o.customer_region),
    ('Customer state',   o.customer_state),
    ('Product category', o.product_category),
    ('Seller state',     o.seller_state),
    ('Payment type',     o.payment_type),
    ('Customer type',    o.customer_type)
) AS d(dimension, segment)
GROUP BY 1, 2, 3;

CREATE VIEW drivers AS
WITH tot AS (            -- month totals the rate contributions are divided by
  SELECT order_month, count(is_late) AS delivered, count(review_score) AS reviewed
  FROM order_facts GROUP BY order_month
), pairs AS (            -- each month with the month before it
  SELECT t.order_month, (t.order_month - interval '1 month')::date AS prev_month
  FROM tot t
  WHERE EXISTS (SELECT 1 FROM tot p WHERE p.order_month = (t.order_month - interval '1 month')::date)
), segs AS (SELECT DISTINCT dimension, segment FROM segment_month),
j AS (                   -- current and previous month side by side (missing = 0)
  SELECT p.order_month, p.prev_month, s.dimension, s.segment,
         COALESCE(c.orders, 0) o1, COALESCE(b.orders, 0) o0,
         COALESCE(c.revenue, 0) r1, COALESCE(b.revenue, 0) r0,
         COALESCE(c.delivered, 0) d1, COALESCE(b.delivered, 0) d0,
         COALESCE(c.late, 0) l1, COALESCE(b.late, 0) l0,
         COALESCE(c.reviewed, 0) v1, COALESCE(b.reviewed, 0) v0,
         COALESCE(c.review_sum, 0) s1, COALESCE(b.review_sum, 0) s0,
         t1.delivered td1, t0.delivered td0, t1.reviewed tv1, t0.reviewed tv0
  FROM pairs p
  CROSS JOIN segs s
  LEFT JOIN segment_month c ON c.order_month = p.order_month AND c.dimension = s.dimension AND c.segment = s.segment
  LEFT JOIN segment_month b ON b.order_month = p.prev_month  AND b.dimension = s.dimension AND b.segment = s.segment
  JOIN tot t1 ON t1.order_month = p.order_month
  JOIN tot t0 ON t0.order_month = p.prev_month
  WHERE c.orders IS NOT NULL OR b.orders IS NOT NULL
)
SELECT order_month, dimension, segment, 'revenue' AS metric,
       round(r1, 2) AS segment_now, round(r0, 2) AS segment_before,
       round(r1 - r0, 2) AS contribution,
       round(CASE WHEN o0 > 0 THEN (o1 - o0) * (r0 / o0) ELSE r1 END, 2) AS volume_effect,
       round((r1 - r0) - CASE WHEN o0 > 0 THEN (o1 - o0) * (r0 / o0) ELSE r1 END, 2) AS value_effect,
       o1 AS orders_now, o0 AS orders_before
FROM j
UNION ALL
SELECT order_month, dimension, segment, 'orders',
       o1, o0, o1 - o0, NULL, NULL, o1, o0
FROM j
UNION ALL
SELECT order_month, dimension, segment, 'late_pct',
       round(100.0 * l1 / NULLIF(d1, 0), 2), round(100.0 * l0 / NULLIF(d0, 0), 2),   -- the segment's own late rate
       round(100.0 * l1 / NULLIF(td1, 0) - 100.0 * l0 / NULLIF(td0, 0), 4),          -- contribution in % points
       NULL, NULL, d1, d0
FROM j
UNION ALL
SELECT order_month, dimension, segment, 'avg_review_score',
       round(s1::numeric / NULLIF(v1, 0), 2), round(s0::numeric / NULLIF(v0, 0), 2),
       round(s1::numeric / NULLIF(tv1, 0) - s0::numeric / NULLIF(tv0, 0), 4),
       NULL, NULL, v1, v0
FROM j;
