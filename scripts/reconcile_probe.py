"""Does Agent Memory itself flag a claimant statement that contradicts an authoritative record?

Loads a copy of the demo record as policy:521585-probe in scope dbrief-probe (so the real demo
record is never touched), remembers two claimant statements with infer "full" (the product's own
extraction and reconciliation), then queries and prints every raw response.
Turn 1 agrees with the record (Saab); turn 4 contradicts it (rear vs side collision).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dbrief.domain import Domain  # noqa: E402
from dbrief.memory import AgentMemory  # noqa: E402

SCOPE = "dbrief-probe"
NAME = "521585-probe"


def show(title: str, data) -> None:
    print(f"\n=== {title}")
    print(json.dumps(data, indent=2)[:3500])


m = AgentMemory.from_env()
if not m:
    sys.exit("SPECTRON_API_KEY missing")
m.timeout = 600
d = Domain.load("insurance")
m.ensure_scope(SCOPE)

triples = [{"entity": {"type": "policy", "name": NAME}, "key": k, "value": v}
           for k, v in d.record_fields(d.demo_record).items() if k in ("auto_make", "collision_type", "incident_hour_of_the_day")]
data, ms = m._call("POST", "/facts", {"text": f"Authoritative record for policy {NAME}", "infer": "triples",
                                      "triples": triples, "scopes": [SCOPE]})
show(f"1. load the record (triples, {ms:.0f} ms)", data)

for n, said in ((1, "I was driving my Saab when it happened."),
                (4, "And it was a rear collision. Somebody went into the back of me.")):
    data, ms = m._call("POST", "/facts", {"text": f"The claimant on policy {NAME} said: {said}",
                                          "infer": "full", "scopes": [SCOPE]})
    show(f"2.{n} remember turn {n} with infer=full ({ms:.0f} ms): {said}", data)

data, ms = m._call("POST", "/query", {"query": f"What collision type was policy {NAME}?", "k": 8, "lens": [SCOPE]})
show(f"3. query ({ms:.0f} ms)", data)

data, ms = m._call("GET", f"/entities/policy/{NAME}")
show(f"4. the entity afterwards ({ms:.0f} ms)", data)
