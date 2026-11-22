"""
Offline test of the LLM code path: replaces the HTTP call with fake Gemini / Anthropic
responses and checks that requests are built and answers are parsed and grounded.
  python tests/mock_llm.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
os.environ.update(GEMINI_API_KEY="test", ANTHROPIC_API_KEY="test", LLM_RPM="100000")

import requests  # noqa: E402

import explain  # noqa: E402
from common import LLM  # noqa: E402

packet = [p for p in json.load(open(ROOT / "data" / "packets.json")) if p["metric"] == "revenue" and p["month"] == "2017-12"][0]
answer = {"headline": "Revenue fell 26.5% after the November peak",
          "explanation": f"Revenue fell from R$ {packet['previous_value']:,.0f} to R$ {packet['value']:,.0f}.",
          "key_drivers": ["SP"], "caveat": "One month only.", "next_questions": ["Check SP."]}
seen = {}


class Resp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def fake_post(url, headers=None, data=None, timeout=None):
    seen["url"], seen["body"] = url, json.loads(data)
    if "googleapis" in url:
        return Resp({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]})
    return Resp({"content": [{"type": "text", "text": "Here you go: " + json.dumps(answer)}]})


requests.post = fake_post
for provider in ("gemini", "anthropic"):
    res = explain.explain(packet, LLM(provider))
    assert res["answer"]["headline"] == answer["headline"], provider
    assert res["grounded"], res["ungrounded_numbers"]
    print(f"{provider:9} OK  url={seen['url'][:60]}...  grounded={res['grounded']}")

bad = dict(answer, explanation="Revenue fell 31% because 4,512 orders were lost.")
print("hallucination caught:", explain.ungrounded(bad["explanation"], packet))
