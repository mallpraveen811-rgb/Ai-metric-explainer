"""
STEP 2: turn the SQL results into "insight packets", one per metric per month.

A packet is a small JSON file holding everything needed to explain one change:
the numbers, how unusual the change is, and the segments that drove it. The LLM
only ever sees a packet, never the database, so it cannot invent data: it can only
explain numbers SQL has already calculated.

  python engine/build_packets.py              # all metrics, all months
  python engine/build_packets.py --schema insight_test   # used by the planted-anomaly test
Output: data/packets.json, data/monthly_metrics.json
"""
import argparse
import json
from collections import defaultdict

from common import DATA, sql

METRICS = {
    "revenue": {"label": "Revenue", "unit": "R$", "good": "up"},
    "orders": {"label": "Orders", "unit": "orders", "good": "up"},
    "late_pct": {"label": "Late deliveries", "unit": "% of delivered orders", "good": "down"},
    "avg_review_score": {"label": "Average review score", "unit": "stars (1-5)", "good": "up"},
}
TOP_N = 3


def r(x, d=2):
    return None if x is None else round(float(x), d)


def build(schema="insight"):
    monthly = sql("SELECT metric, order_month::text AS month, value, prev_value, change, change_pct, z_score "
                  "FROM monthly_metrics ORDER BY metric, order_month", schema)
    drivers = sql("SELECT order_month::text AS month, metric, dimension, segment, segment_now, segment_before, "
                  "contribution, volume_effect, value_effect, orders_now, orders_before "
                  "FROM drivers WHERE contribution <> 0", schema)

    by_key = defaultdict(list)
    for d in drivers:
        by_key[(d["metric"], d["month"])].append(d)

    # segment-level anomaly score: is this segment's contribution unusual compared with
    # its own contributions in the previous 6 months? Catches problems hidden inside a
    # total that looks normal (e.g. one state collapses while others grow).
    months = sorted({m["month"] for m in monthly})
    hist = defaultdict(dict)
    for d in drivers:
        hist[(d["metric"], d["dimension"], d["segment"])][d["month"]] = float(d["contribution"])

    def seg_z(metric, dim, seg, month):
        h = hist[(metric, dim, seg)]
        i = months.index(month)
        prev = [h.get(mo, 0.0) for mo in months[max(1, i - 6):i]]   # month 0 has no change
        if len(prev) < 4:
            return None
        mean = sum(prev) / len(prev)
        sd = (sum((x - mean) ** 2 for x in prev) / (len(prev) - 1)) ** 0.5
        floor = 0.05 * max(abs(x) for x in prev + [h.get(month, 0.0)])  # avoid dividing by ~0
        return round((h.get(month, 0.0) - mean) / max(sd, floor, 1e-9), 2)
    mm = {(m["metric"], m["month"]): m for m in monthly}

    packets = []
    for m in monthly:
        if m["change"] is None:
            continue
        key = (m["metric"], m["month"])
        meta = METRICS[m["metric"]]
        change = float(m["change"])
        p = {
            "metric": m["metric"], "metric_label": meta["label"], "unit": meta["unit"],
            "better_when": meta["good"], "month": m["month"][:7],
            "value": r(m["value"]), "previous_value": r(m["prev_value"]),
            "change": r(change), "change_pct": r(m["change_pct"]),
            "z_score": r(m["z_score"]),
            "is_unusual": m["z_score"] is not None and abs(float(m["z_score"])) >= 2,
            "note": "Contributions inside one dimension add up to the total change. Dimensions overlap "
                    "(a state is inside a region), so shares must not be added across dimensions.",
        }
        if m["metric"] == "revenue":          # split revenue change into orders effect + order-size effect
            o1, o0 = mm[("orders", m["month"])]["value"], mm[("orders", m["month"])]["prev_value"]
            v1, v0 = float(m["value"]), float(m["prev_value"])
            aov1, aov0 = v1 / o1, v0 / o0
            p["average_order_value"] = r(aov1)
            p["previous_average_order_value"] = r(aov0)
            p["volume_effect"] = r((o1 - o0) * aov0)      # change in orders x old order value
            p["value_effect"] = r(o1 * (aov1 - aov0))     # new orders x change in order value
        dims = defaultdict(list)
        for d in by_key[key]:
            dims[d["dimension"]].append(d)
        p["drivers"] = {}
        for dim, rows in dims.items():
            def item(d):
                it = {"segment": d["segment"], "contribution": r(d["contribution"], 4 if m["metric"] in ("late_pct", "avg_review_score") else 2),
                      "share_of_change_pct": r(100 * float(d["contribution"]) / change, 1) if change else None,
                      "segment_value_now": r(d["segment_now"]), "segment_value_before": r(d["segment_before"]),
                      "segment_z_score": seg_z(m["metric"], dim, d["segment"], m["month"])}
                if m["metric"] == "revenue":
                    it["volume_effect"], it["value_effect"] = r(d["volume_effect"]), r(d["value_effect"])
                return it
            same = sorted([d for d in rows if (float(d["contribution"]) > 0) == (change > 0)],
                          key=lambda d: -abs(float(d["contribution"])))
            against = sorted([d for d in rows if (float(d["contribution"]) > 0) != (change > 0)],
                             key=lambda d: -abs(float(d["contribution"])))
            p["drivers"][dim] = {"pushed_with_the_change": [item(d) for d in same[:TOP_N]],
                                 "pushed_against_the_change": [item(d) for d in against[:TOP_N]]}
        # alerts: big, unusual segment moves (|segment z| >= 4 and >= 20% of the change)
        p["segment_alerts"] = [
            {"dimension": dim, "segment": it["segment"], "segment_z_score": it["segment_z_score"],
             "share_of_change_pct": it["share_of_change_pct"], "contribution": it["contribution"]}
            for dim, dd in p["drivers"].items()
            for it in dd["pushed_with_the_change"] + dd["pushed_against_the_change"]
            if it["segment_z_score"] is not None and abs(it["segment_z_score"]) >= 4
            and it["share_of_change_pct"] is not None and abs(it["share_of_change_pct"]) >= 20]
        p["needs_attention"] = p["is_unusual"] or bool(p["segment_alerts"])
        packets.append(p)
    return packets, monthly


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="insight")
    ap.add_argument("--out", default=str(DATA / "packets.json"))
    a = ap.parse_args()
    packets, monthly = build(a.schema)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(packets, f, ensure_ascii=False, indent=1)
    if a.schema == "insight":
        with open(DATA / "monthly_metrics.json", "w", encoding="utf-8") as f:
            json.dump(monthly, f, indent=1)
    unusual = [f"{p['metric']} {p['month']}" for p in packets if p["is_unusual"]]
    hidden = [f"{p['metric']} {p['month']}" for p in packets if p["segment_alerts"] and not p["is_unusual"]]
    print(f"{len(packets)} packets -> {a.out}\nUnusual totals (|z| >= 2): {', '.join(unusual)}"
          f"\nNormal totals hiding an unusual segment: {len(hidden)}")


if __name__ == "__main__":
    main()
