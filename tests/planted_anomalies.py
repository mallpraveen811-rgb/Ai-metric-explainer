"""
PLANTED-ANOMALY TEST: does the engine find a cause we know is there?

Real data has no answer key, so we make one: copy the orders into a test schema,
deliberately break one segment in one month, re-run the whole engine on the copy,
and check that the planted segment comes out as the #1 driver.

  python tests/planted_anomalies.py
Output: results/planted_anomaly_test.md / .json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
from build_packets import build  # noqa: E402
from common import RESULTS, run_sql_file, sql  # noqa: E402

# each case: (name, metric, month, dimension, segment, SQL that plants the shock)
# md5(order_id) picks a fixed, repeatable "random" subset of orders
CASES = [
    ("Health & beauty sales collapse", "revenue", "2018-05", "Product category", "health_beauty",
     "DELETE FROM order_facts WHERE product_category='health_beauty' AND order_month='2018-05-01' "
     "AND md5(order_id) < '99'"),
    ("Carrier failure in Bahia", "late_pct", "2018-07", "Customer state", "BA",
     "UPDATE order_facts SET is_late = true WHERE customer_state='BA' AND order_month='2018-07-01' "
     "AND is_late IS NOT NULL AND md5(order_id) < 'b3'"),
    ("Bad batch from Paraná sellers", "avg_review_score", "2018-06", "Seller state", "PR",
     "UPDATE order_facts SET review_score = 1 WHERE seller_state='PR' AND order_month='2018-06-01' "
     "AND review_score IS NOT NULL AND md5(order_id) < '99'"),
    # same shock during a real crisis (Feb 2018 delivery problems): expected to be outranked
    ("Paraná batch during Feb 2018 crisis", "avg_review_score", "2018-02", "Seller state", "PR",
     "UPDATE order_facts SET review_score = 1 WHERE seller_state='PR' AND order_month='2018-02-01' "
     "AND review_score IS NOT NULL AND md5(order_id) < '99'"),
    ("Boleto payment promotion", "orders", "2017-09", "Payment type", "boleto",
     "INSERT INTO order_facts SELECT order_id || '-dup', order_month, customer_region, customer_state, "
     "product_category, seller_state, payment_type, customer_type, order_value, is_late, review_score "
     "FROM order_facts WHERE payment_type='boleto' AND order_month='2017-09-01' AND md5(order_id) < 'cc'"),
    ("Watch prices tripled", "revenue", "2018-03", "Product category", "watches_gifts",
     "UPDATE order_facts SET order_value = order_value * 3 WHERE product_category='watches_gifts' "
     "AND order_month='2018-03-01'"),
]


def main():
    results = []
    for name, metric, month, dim, seg, shock in CASES:
        sql("DROP SCHEMA IF EXISTS insight_test CASCADE; CREATE SCHEMA insight_test; "
            "CREATE TABLE insight_test.order_facts AS SELECT * FROM insight.order_facts; " + shock + ";",
            schema="insight_test")
        run_sql_file(ROOT / "sql" / "02_monthly_metrics.sql", schema="insight_test")
        run_sql_file(ROOT / "sql" / "03_drivers.sql", schema="insight_test")
        packets, _ = build("insight_test")
        p = next(x for x in packets if x["metric"] == metric and x["month"] == month)
        top = p["drivers"][dim]["pushed_with_the_change"]
        found = bool(top) and top[0]["segment"] == seg
        rank = next((i + 1 for i, x in enumerate(top) if x["segment"] == seg), None)
        res = {"case": name, "metric": metric, "month": month, "dimension": dim, "planted_segment": seg,
               "found_as_top_driver": found, "rank": rank,
               "share_of_change_pct": top[0]["share_of_change_pct"] if found else None,
               "change_pct": p["change_pct"], "z_score": p["z_score"], "flagged_unusual": p["is_unusual"],
               "segment_z_score": next((x["segment_z_score"] for x in top if x["segment"] == seg), None),
               "segment_alert": any(al["segment"] == seg for al in p["segment_alerts"]),
               "needs_attention": p["needs_attention"]}
        if metric == "revenue":
            res["volume_effect"], res["value_effect"] = top[0].get("volume_effect"), top[0].get("value_effect")
        results.append(res)
        print(f"{'PASS' if found else 'MISS'}  {name:36} top {dim.lower()} = {top[0]['segment'] if top else '-'}"
              f" (planted rank {rank}), total z={p['z_score']}, segment alert={res['segment_alert']}")
    sql("DROP SCHEMA IF EXISTS insight_test CASCADE", schema="insight_test")

    passed = sum(r["found_as_top_driver"] for r in results)
    flagged = sum(bool(r["flagged_unusual"]) for r in results)
    alerted = sum(bool(r["needs_attention"]) for r in results)
    seg_alerted = sum(bool(r["segment_alert"]) for r in results)
    with open(RESULTS / "planted_anomaly_test.json", "w") as f:
        json.dump(results, f, indent=1)
    with open(RESULTS / "planted_anomaly_test.md", "w") as f:
        f.write(f"# Planted-anomaly test\n\nPlanted driver found as #1: **{passed}/{len(results)}**  \n"
                f"Total change flagged unusual (|z| >= 2): **{flagged}/{len(results)}**  \n"
                f"Planted segment raised a segment alert: **{seg_alerted}/{len(results)}**  \n"
                f"Month flagged for attention (total or any segment alert): **{alerted}/{len(results)}**\n\n"
                "| Case | Metric | Month | Planted | Rank | Share of change | Total z | Segment z | Segment alert |\n|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['case']} | {r['metric']} | {r['month']} | {r['dimension']}: {r['planted_segment']} | "
                    f"{r['rank'] or '-'} | {r['share_of_change_pct'] if r['share_of_change_pct'] is not None else '-'}% | {r['z_score']} | "
                    f"{r['segment_z_score']} | {'yes' if r['segment_alert'] else 'no'} |\n")
    print(f"\nFound {passed}/{len(results)} planted drivers as #1; total-level flag {flagged}/{len(results)}; "
          f"planted segment alerted {seg_alerted}/{len(results)}; month flagged {alerted}/{len(results)}.")


if __name__ == "__main__":
    main()
