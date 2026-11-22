-- =====================================================================
-- 01_order_facts.sql  |  One clean row per order: the only input the engine reads
-- Built on the Olist star schema from the Complaint Cost Engine project
-- (dw.fact_orders + dimensions). Every later step reads only this view, so the
-- engine can be pointed at any other order table with the same columns.
--
-- Scope: Jan 2017 - Aug 2018 (months before and after have only a handful of
-- orders and would create fake "spikes"); cancelled / unavailable orders removed.
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS insight;
DROP VIEW IF EXISTS insight.order_facts CASCADE;

CREATE VIEW insight.order_facts AS
SELECT f.order_id,
       date_trunc('month', f.order_date)::date                      AS order_month,
       COALESCE(st.region, 'Unknown')                                AS customer_region,
       COALESCE(c.state, '??')                                       AS customer_state,
       COALESCE(p.category, 'unknown')                               AS product_category,
       COALESCE(s.state, '??')                                       AS seller_state,
       COALESCE(f.main_payment_type, 'unknown')                      AS payment_type,
       CASE WHEN f.customer_order_seq = 1 THEN 'New customer'
            ELSE 'Returning customer' END                            AS customer_type,
       f.order_value,
       f.is_late,          -- NULL when the order was not delivered
       f.review_score      -- NULL when there is no review
FROM dw.fact_orders f
LEFT JOIN dw.dim_customer c ON c.customer_key = f.customer_key
LEFT JOIN dw.dim_state   st ON st.state       = c.state
LEFT JOIN dw.dim_product  p ON p.product_key  = f.product_key
LEFT JOIN dw.dim_seller   s ON s.seller_key   = f.seller_key
WHERE f.order_status NOT IN ('canceled', 'unavailable')
  AND f.order_date >= DATE '2017-01-01'
  AND f.order_date <  DATE '2018-09-01';
