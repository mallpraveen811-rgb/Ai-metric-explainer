"""
Shared helpers: settings from .env, running SQL through psql, and small LLM clients.

Only the standard library + requests + pandas are used, so every request sent to an
LLM and every answer received is plain, readable JSON.
"""
import csv
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"


def load_env(path=ROOT / ".env"):
    """Minimal .env reader (KEY=value lines)."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sql(query, schema="insight"):
    """Run a query with psql and return a list of dicts (numbers converted).
    Connection comes from the usual PGHOST / PGUSER / PGPASSWORD / PGDATABASE variables."""
    load_env()
    env = dict(os.environ, PGOPTIONS=f"-c search_path={schema},public -c client_min_messages=warning")
    out = subprocess.run(["psql", "-X", "--csv", "-v", "ON_ERROR_STOP=1", "-c", query],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise SystemExit(f"psql failed: {out.stderr.strip()}")
    rows = list(csv.DictReader(io.StringIO(out.stdout)))
    for r in rows:
        for k, v in r.items():
            if v == "":
                r[k] = None
            elif re.fullmatch(r"-?\d+", v):
                r[k] = int(v)
            elif re.fullmatch(r"-?\d+\.\d+", v):
                r[k] = float(v)
    return rows


def run_sql_file(path, schema="insight"):
    env = dict(os.environ, PGOPTIONS="-c client_min_messages=warning")
    out = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"schema={schema}", "-f", str(path)],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise SystemExit(f"{path} failed: {out.stderr.strip()}")


# --------------------------------------------------------------------------- LLM clients
class LLM:
    """One interface, three back ends:
       gemini    - Google Gemini REST API (free tier available)   needs GEMINI_API_KEY
       anthropic - Claude via the Anthropic Messages API           needs ANTHROPIC_API_KEY
       template  - no AI at all: a fixed-sentence baseline used for tests and comparison"""

    def __init__(self, provider=None, model=None, rpm=None):
        load_env()
        self.provider = provider or os.getenv("LLM_PROVIDER", "gemini")
        self.min_gap = 60.0 / float(rpm or os.getenv("LLM_RPM", "10"))
        self._last = 0.0
        if self.provider == "gemini":
            self.key = os.getenv("GEMINI_API_KEY")
            self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        elif self.provider == "anthropic":
            self.key = os.getenv("ANTHROPIC_API_KEY")
            self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
        elif self.provider == "template":
            self.key, self.model = "none", "template-v1"
        else:
            raise SystemExit(f"Unknown provider {self.provider}")
        if not self.key:
            raise SystemExit(f"No API key for {self.provider}. Copy .env.example to .env and add it, "
                             "or run with --provider template.")

    def _wait(self):
        gap = self.min_gap - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def json(self, system, user, schema):
        """Send one request and return (parsed JSON answer, raw text)."""
        for attempt in range(6):
            self._wait()
            try:
                if self.provider == "gemini":
                    r = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                        headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
                        data=json.dumps({
                            "systemInstruction": {"parts": [{"text": system}]},
                            "contents": [{"role": "user", "parts": [{"text": user}]}],
                            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
                        }), timeout=120)
                    if r.status_code == 200:
                        parts = r.json()["candidates"][0]["content"]["parts"]
                        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                        return json.loads(text), text
                else:  # anthropic
                    r = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        data=json.dumps({
                            "model": self.model, "max_tokens": 1500,
                            "system": system + "\n\nReply with ONLY a JSON object matching this schema, no other text:\n"
                                      + json.dumps(schema),
                            "messages": [{"role": "user", "content": user}],
                        }), timeout=120)
                    if r.status_code == 200:
                        text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
                        text = text[text.find("{"): text.rfind("}") + 1]
                        return json.loads(text), text
            except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
                print(f"  retrying after error: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504, 529):
                print(f"  HTTP {r.status_code}; waiting {2 ** (attempt + 1)}s")
                time.sleep(2 ** (attempt + 1))
            else:
                raise SystemExit(f"{self.provider} API error {r.status_code}: {r.text[:400]}")
        raise RuntimeError("LLM call failed after retries")
