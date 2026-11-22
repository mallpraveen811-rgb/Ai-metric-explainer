"""
BRING YOUR OWN DATA: run the whole "why did it move?" analysis on any CSV file.
No database needed: pandas does what the SQL files do for the Olist data.

  python engine/analyze_csv.py samples/superstore_orders.csv --metric Sales
  python engine/analyze_csv.py samples/superstore_orders.csv --metric Profit --explain
  python engine/analyze_csv.py my_customers.csv --metric rows --date signup_date --period W
  python engine/analyze_csv.py file.csv --metric review_score --agg mean --better down

What it does
  1. Profile: row count, every column's type (date / number / category / text), missing %, distinct values.
  2. Choose: a date column (auto-detected), a metric (a numeric column, or "rows" to count rows)
     and the categorical columns to break it down by (2-100 distinct values, auto-selected).
  3. Per period (month by default): metric value, change, z-score vs the previous 6 changes.
  4. Per period and segment: exact contribution to the change (sum, count or average).
  5. Writes results/<file>_profile.json and results/<file>_packets.json; with --explain, asks
     the LLM to explain every unusual change and runs the grounding check.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from common import LLM, RESULTS

MAX_DIMS = 6


# ------------------------------------------------------------------ 1. profile
def profile(df):
    cols = []
    for c in df.columns:
        s = df[c]
        nonnull = s.dropna()
        nonnull = nonnull[nonnull.astype(str).str.strip() != ""]
        kind = "text"
        if len(nonnull):
            num = pd.to_numeric(nonnull, errors="coerce")
            looks_date = nonnull.astype(str).str.contains(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}", regex=True).mean() > 0.9
            if looks_date and pd.to_datetime(nonnull, errors="coerce").notna().mean() > 0.9:
                kind = "date"
            elif num.notna().mean() > 0.95:
                kind = "number"
            elif nonnull.nunique() <= 100:
                kind = "category"
        info = {"column": c, "type": kind, "missing_pct": round(100 * (1 - len(nonnull) / max(len(df), 1)), 1),
                "distinct": int(nonnull.nunique())}
        if kind == "number":
            n = pd.to_numeric(nonnull, errors="coerce")
            info.update(min=round(float(n.min()), 2), max=round(float(n.max()), 2), mean=round(float(n.mean()), 2))
        if kind in ("category", "text"):
            info["top_values"] = nonnull.astype(str).value_counts().head(3).to_dict()
        cols.append(info)
    return {"rows": int(len(df)), "columns": cols}


# ------------------------------------------------------------------ 2-4. periods, changes, contributions
def analyze(df, date_col, metric, agg, dims, period="M", better="up"):
    d = df.copy()
    d["_date"] = pd.to_datetime(d[date_col], errors="coerce")
    d = d[d["_date"].notna()]
    d["_period"] = d["_date"].dt.to_period(period).astype(str)
    d["_x"] = 1.0 if metric == "rows" else pd.to_numeric(d[metric], errors="coerce")
    if agg == "mean":                       # an average only uses rows that have a value
        d = d[d["_x"].notna()]
    d["_x"] = d["_x"].fillna(0.0)

    tot = d.groupby("_period").agg(n=("_x", "size"), s=("_x", "sum")).sort_index()
    # drop thin edge periods (< 10% of the median size) that create fake spikes
    keep = tot["n"] >= 0.1 * tot["n"].median()
    tot = tot[keep]
    d = d[d["_period"].isin(tot.index)]
    value = tot["s"] if agg == "sum" else tot["n"] if agg == "count" else tot["s"] / tot["n"]
    change = value.diff()
    z = pd.Series(index=value.index, dtype=float)
    for i in range(len(value)):
        prev = change.iloc[max(1, i - 6):i].dropna()
        if len(prev) >= 4 and prev.std() > 0:
            z.iloc[i] = (change.iloc[i] - prev.mean()) / prev.std()

    seg = {}
    for dim in dims:
        g = d.groupby(["_period", d[dim].fillna("(blank)").astype(str)])["_x"].agg(["size", "sum"])
        seg[dim] = g.unstack(fill_value=0)       # columns: (size|sum, segment)

    periods = list(value.index)
    contrib_hist = {}                             # (dim, segment) -> {period: contribution}
    raw = {}
    for i in range(1, len(periods)):
        p1, p0 = periods[i], periods[i - 1]
        for dim in dims:
            n1, n0 = seg[dim]["size"].loc[p1], seg[dim]["size"].loc[p0]
            s1, s0 = seg[dim]["sum"].loc[p1], seg[dim]["sum"].loc[p0]
            if agg == "sum":
                c, now, before = s1 - s0, s1, s0
            elif agg == "count":
                c, now, before = n1 - n0, n1, n0
            else:                                 # average: each segment's share of the overall average
                c = s1 / tot["n"][p1] - s0 / tot["n"][p0]
                now, before = (s1 / n1.replace(0, np.nan)), (s0 / n0.replace(0, np.nan))
            for sname in c.index:
                if n1[sname] == 0 and n0[sname] == 0:
                    continue
                contrib_hist.setdefault((dim, sname), {})[p1] = float(c[sname])
                raw[(p1, dim, sname)] = (float(c[sname]), float(now[sname]) if pd.notna(now[sname]) else None,
                                         float(before[sname]) if pd.notna(before[sname]) else None)

    def seg_z(dim, sname, p):
        h = contrib_hist.get((dim, sname), {})
        i = periods.index(p)
        prev = [h.get(q, 0.0) for q in periods[max(1, i - 6):i]]
        if len(prev) < 4:
            return None
        mean = sum(prev) / len(prev)
        sd = (sum((x - mean) ** 2 for x in prev) / (len(prev) - 1)) ** 0.5
        floor = 0.05 * max(abs(x) for x in prev + [h.get(p, 0.0)])
        return round((h.get(p, 0.0) - mean) / max(sd, floor, 1e-9), 2)

    label = ("Number of rows" if metric == "rows" else
             {"sum": "Total", "count": "Count of", "mean": "Average"}[agg] + f" {metric}")
    packets = []
    for i in range(1, len(periods)):
        p1, p0 = periods[i], periods[i - 1]
        ch = float(change.iloc[i])
        pk = {"metric": "custom", "metric_label": label, "unit": metric, "better_when": better, "month": p1,
              "value": round(float(value.iloc[i]), 4), "previous_value": round(float(value.iloc[i - 1]), 4),
              "change": round(ch, 4),
              "change_pct": round(100 * ch / float(value.iloc[i - 1]), 2) if value.iloc[i - 1] else None,
              "z_score": None if pd.isna(z.iloc[i]) else round(float(z.iloc[i]), 2)}
        pk["is_unusual"] = pk["z_score"] is not None and abs(pk["z_score"]) >= 2
        if agg == "sum" and metric != "rows":      # rows x average-per-row split
            n1, n0, s1, s0 = tot["n"][p1], tot["n"][p0], tot["s"][p1], tot["s"][p0]
            pk["rows"], pk["previous_rows"] = int(n1), int(n0)
            pk["volume_effect"] = round(float((n1 - n0) * (s0 / n0)), 2)
            pk["value_effect"] = round(float(n1 * (s1 / n1 - s0 / n0)), 2)
        pk["drivers"] = {}
        for dim in dims:
            items = [(s, *raw[(p1, dim, s)]) for (pp, dd, s) in raw if pp == p1 and dd == dim]
            def item(t):
                s, c, now, before = t
                return {"segment": s, "contribution": round(c, 4),
                        "share_of_change_pct": round(100 * c / ch, 1) if ch else None,
                        "segment_value_now": None if now is None else round(now, 2),
                        "segment_value_before": None if before is None else round(before, 2),
                        "segment_z_score": seg_z(dim, s, p1)}
            same = sorted([t for t in items if t[1] != 0 and (t[1] > 0) == (ch > 0)], key=lambda t: -abs(t[1]))[:3]
            against = sorted([t for t in items if t[1] != 0 and (t[1] > 0) != (ch > 0)], key=lambda t: -abs(t[1]))[:3]
            pk["drivers"][dim] = {"pushed_with_the_change": [item(t) for t in same],
                                  "pushed_against_the_change": [item(t) for t in against]}
        pk["segment_alerts"] = [
            {"dimension": dim, "segment": it["segment"], "segment_z_score": it["segment_z_score"],
             "share_of_change_pct": it["share_of_change_pct"], "contribution": it["contribution"]}
            for dim, dd in pk["drivers"].items() for it in dd["pushed_with_the_change"] + dd["pushed_against_the_change"]
            if it["segment_z_score"] is not None and abs(it["segment_z_score"]) >= 4
            and it["share_of_change_pct"] is not None and abs(it["share_of_change_pct"]) >= 20]
        packets.append(pk)
    return packets


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--date", help="date column (auto-detected if omitted)")
    ap.add_argument("--metric", default="rows", help='numeric column to analyze, or "rows" to count rows')
    ap.add_argument("--agg", choices=["sum", "count", "mean"], help="default: sum for a column, count for rows")
    ap.add_argument("--dims", help="comma-separated breakdown columns (auto if omitted)")
    ap.add_argument("--period", default="M", help="M = month, W = week, D = day, Q = quarter")
    ap.add_argument("--better", choices=["up", "down"], default="up", help="is an increase good or bad?")
    ap.add_argument("--explain", action="store_true", help="ask the LLM to explain every unusual change")
    ap.add_argument("--provider", choices=["gemini", "anthropic", "template"])
    a = ap.parse_args()

    df = pd.read_csv(a.file, low_memory=False)
    prof = profile(df)
    types = {c["column"]: c for c in prof["columns"]}
    date_col = a.date or next((c for c, t in types.items() if t["type"] == "date"), None)
    if not date_col:
        raise SystemExit("No date column found. Pass --date COLUMN.")
    agg = a.agg or ("count" if a.metric == "rows" else "sum")
    dims = a.dims.split(",") if a.dims else [c for c, t in types.items()
                                             if t["type"] == "category" and 2 <= t["distinct"] <= 100 and c != a.metric][:MAX_DIMS]
    print(f"{prof['rows']:,} rows · date: {date_col} · metric: {agg}({a.metric}) · breakdowns: {', '.join(dims)}")

    packets = analyze(df, date_col, a.metric, agg, dims, a.period, a.better)
    stem = Path(a.file).stem
    prof.update(date_column=date_col, metric=a.metric, agg=agg, dimensions=dims, period=a.period)
    json.dump(prof, open(RESULTS / f"{stem}_profile.json", "w"), indent=1, default=str)
    json.dump(packets, open(RESULTS / f"{stem}_packets.json", "w"), indent=1, default=str)
    unusual = [p for p in packets if p["is_unusual"]]
    print(f"{len(packets)} periods analyzed; unusual changes: " +
          (", ".join(f"{p['month']} ({p['change_pct']}%, z={p['z_score']})" for p in unusual) or "none"))

    if a.explain and unusual:
        import explain
        llm = LLM(a.provider)
        out = [explain.explain(p, llm) for p in unusual]
        for r in out:
            print(f"\n{r['month']}: {r['answer']['headline']}\n  {r['answer']['explanation']}\n  grounding: "
                  f"{'passed' if r['grounded'] else r['ungrounded_numbers']}")
        json.dump(out, open(RESULTS / f"{stem}_explanations_{llm.provider}.json", "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
