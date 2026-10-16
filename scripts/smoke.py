"""Agent Memory smoke test: scope -> one triple -> query -> trace -> entity. Prints raw responses.

The API key is read from the environment or .env and is never printed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("SPECTRON_URL", "https://srv1.spectron.aws-usw2.surreal.cloud")
CTX = os.environ.get("SPECTRON_CONTEXT_ID", "06gkauiei1u412cclcdhr470co")


def key() -> str:
    k = os.environ.get("SPECTRON_API_KEY")
    if not k and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("SPECTRON_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        sys.exit("SPECTRON_API_KEY missing: add it to .env")
    return k


KEY = key()


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"{URL}/api/v1/{CTX}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    ms = (time.perf_counter() - t0) * 1000
    raw = raw.replace(KEY, "<redacted>")
    print(f"\n=== {method} {path}  ->  HTTP {status}  ({ms:.0f} ms)")
    if body is not None:
        print("request:", json.dumps(body))
    try:
        data = json.loads(raw)
        print(json.dumps(data, indent=2)[:4000])
        return status, data
    except ValueError:
        print(raw[:2000])
        return status, raw


if __name__ == "__main__":
    steps = sys.argv[1:] or ["scope", "fact", "query", "trace", "entity"]
    st = {}
    if "scope" in steps:
        call("POST", "/scopes", {"path": "dbrief"})
    if "fact" in steps:
        call("POST", "/facts", {
            "text": "Policy 999001 incident_hour_of_the_day is 5.",
            "infer": "triples",
            "triples": [{"entity": {"type": "policy", "name": "999001"},
                         "key": "incident_hour_of_the_day", "value": "5"}],
            "scopes": ["dbrief"],
        })
    if "query" in steps:
        _, st["q"] = call("POST", "/query", {"query": "policy 999001 incident hour", "k": 5, "lens": ["dbrief"]})
    if "trace" in steps and isinstance(st.get("q"), dict):
        tid = (st["q"].get("trace") or {}).get("traceId")
        if tid:
            call("GET", f"/traces/{tid}")
        else:
            print("\n(no trace.traceId in query response)")
    if "entity" in steps:
        call("GET", "/entities/policy/999001")
