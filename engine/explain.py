"""
STEP 3: ask an LLM to explain a metric change, then check its answer.

  python engine/explain.py --metric revenue --month 2017-12          # one change
  python engine/explain.py --unusual                                 # every unusual change
  python engine/explain.py --unusual --provider template             # no AI: fixed-sentence baseline
  python engine/explain.py --unusual --provider anthropic            # Claude instead of Gemini

Output: results/explanations_<provider>.json and a readable results/explanations_<provider>.md

Guardrails:
  1. The model only sees the insight packet (numbers SQL already computed).
  2. It must answer in a fixed JSON shape (headline, explanation, drivers, caveats, next questions).
  3. Grounding check: every number in its answer must appear in the packet. Anything
     else is flagged as a possible hallucination and reported.
"""
import argparse
import json
import re

from common import DATA, LLM, RESULTS

SYSTEM = """You are a senior data analyst. You explain why a business metric changed from one period
(usually a month) to the next, for managers who do not read SQL.

You receive one JSON "insight packet" with the metric, its change, a z_score (how unusual the
change is compared with the previous 6 monthly changes; |z| >= 2 is unusual) and, for several
dimensions, the segments that pushed the metric in the direction of the change or against it.
Each segment's "contribution" is its exact part of the total change; contributions within one
dimension add up to the total. For rate metrics (late_pct, avg_review_score), contribution is in
percentage points or score points, and segment_value_now/before is the segment's own rate.

Rules:
- Use ONLY numbers that appear in the packet. Do not compute new totals, averages or percentages.
  You may round a number, but write it in full (R$ 87,864, not R$ 88K).
- Name the 2-3 segments that explain most of the change, with their share_of_change_pct. Prefer the specific
  dimensions (customer state, product category, seller state). Customer type and payment type have only a few
  segments, so one of them always explains a large share: mention them only when that is itself notable.
- Do not add shares across dimensions (they overlap).
- Say whether the change is unusual using the z_score, in plain words. If segment_alerts is not empty, also
  mention the segment whose own move was unusual (segment_z_score), even when the total looks normal.
- If volume_effect and value_effect are present, say whether the change came from the number of orders/rows
  (volume_effect) or the size of each (value_effect).
- Describe what the data shows; do not claim causes the data cannot show. Suggest checks instead.
- Plain business English, no markdown."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "headline": {"type": "STRING", "description": "max 14 words"},
        "explanation": {"type": "STRING", "description": "3-5 sentences"},
        "key_drivers": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "2-3 short bullet points"},
        "caveat": {"type": "STRING", "description": "one limitation of this reading"},
        "next_questions": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "2 checks an analyst should run next"},
    },
    "required": ["headline", "explanation", "key_drivers", "caveat", "next_questions"],
}


# ------------------------------------------------------------------ grounding check
def allowed_numbers(obj):
    found = set()

    def add(x):
        x = abs(float(x))
        for d in (0, 1, 2, 3, 4):
            found.add(f"{round(x, d):.{d}f}")
        found.add(str(int(x)))

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, bool):
            return
        elif isinstance(o, (int, float)):
            add(o)
        elif isinstance(o, str):
            for n in re.findall(r"\d+(?:\.\d+)?", o):
                add(n)
    walk(obj)
    found.update({"1", "2", "3", "4", "5", "6"})   # small counts like "3 segments", "6 months", "1-5 stars"
    return found


def ungrounded(text, packet):
    ok = allowed_numbers(packet)
    bad = []
    text = re.sub(r"\b\d{4}-\d{2}(-\d{2})?\b", " ", text)   # dates like 2017-12 are not figures
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        n = raw.replace(",", "").rstrip(".")
        if n in ok or (n.isdigit() and 2016 <= int(n) <= 2019):
            continue
        if "." in n and (n.rstrip("0").rstrip(".") in ok):
            continue
        bad.append(raw)
    return bad


# ------------------------------------------------------------------ template baseline (no AI)
def template(p):
    direction = "rose" if p["change"] > 0 else "fell"
    dim = next((k for k in ("Customer state", "Product category", "Seller state") if k in p["drivers"]),
               next(iter(p["drivers"])))
    d = p["drivers"][dim]
    top = d["pushed_with_the_change"][0]
    return {
        "headline": f"{p['metric_label']} {direction} {abs(p['change_pct'])}% in {p['month']}",
        "explanation": f"{p['metric_label']} {direction} from {p['previous_value']} to {p['value']}. "
                       f"The largest driver by {dim.lower()} was {top['segment']} ({top['share_of_change_pct']}% of the change).",
        "key_drivers": [f"{x['segment']}: {x['share_of_change_pct']}%" for x in d["pushed_with_the_change"]],
        "caveat": "Template text: it lists numbers but does not interpret them.",
        "next_questions": ["Check the top segment's orders.", "Compare with the same month last year."],
    }


def explain(packet, llm):
    if llm.provider == "template":
        ans = template(packet)
    else:
        ans, _ = llm.json(SYSTEM, json.dumps(packet, ensure_ascii=False), SCHEMA)
    text = " ".join([ans.get("headline", ""), ans.get("explanation", ""), " ".join(ans.get("key_drivers", [])),
                     ans.get("caveat", ""), " ".join(ans.get("next_questions", []))])
    bad = ungrounded(text, packet)
    return {"metric": packet["metric"], "month": packet["month"], "provider": llm.provider, "model": llm.model,
            "answer": ans, "ungrounded_numbers": bad, "grounded": not bad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric")
    ap.add_argument("--month")
    ap.add_argument("--unusual", action="store_true", help="explain every change with |z| >= 2")
    ap.add_argument("--provider", choices=["gemini", "anthropic", "template"])
    ap.add_argument("--packets", default=str(DATA / "packets.json"))
    a = ap.parse_args()

    packets = json.load(open(a.packets, encoding="utf-8"))
    if a.unusual:
        todo = [p for p in packets if p["is_unusual"]]
    else:
        todo = [p for p in packets if p["metric"] == a.metric and p["month"] == a.month]
    if not todo:
        raise SystemExit("No matching packet. Use --metric revenue|orders|late_pct|avg_review_score --month YYYY-MM")

    llm = LLM(a.provider)
    out = []
    for p in todo:
        res = explain(p, llm)
        out.append(res)
        flag = "OK" if res["grounded"] else f"CHECK numbers {res['ungrounded_numbers']}"
        print(f"{p['metric']:>17} {p['month']}  [{flag}]  {res['answer']['headline']}")

    name = f"explanations_{llm.provider}"
    with open(RESULTS / f"{name}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(RESULTS / f"{name}.md", "w", encoding="utf-8") as f:
        for res in out:
            ans = res["answer"]
            f.write(f"## {res['metric']} · {res['month']}\n\n**{ans['headline']}**\n\n{ans['explanation']}\n\n")
            f.write("".join(f"- {k}\n" for k in ans["key_drivers"]))
            f.write(f"\n*Caveat:* {ans['caveat']}\n\n*Next:* " + " / ".join(ans["next_questions"]) + "\n\n")
            f.write(f"Grounding check: {'passed' if res['grounded'] else 'flagged ' + str(res['ungrounded_numbers'])}\n\n---\n\n")
    ok = sum(r["grounded"] for r in out)
    print(f"\n{ok}/{len(out)} answers passed the grounding check -> results/{name}.json")


if __name__ == "__main__":
    main()
