"""Deterministic stand-ins for the LLMs. No network, no API calls.

Used for --offline play, for the stage fallback when the wifi dies, and as the regression test.
Only the *language* is scripted: memory is still real (local SurrealDB memory: recall, inspect,
traces), claims still go through fn::report and the check_claim event, time travel is still real.
"""
from __future__ import annotations

from .fields import FIELDS


def C(field: str, value: str, quote: str, revises: bool = False, confidence: float = 0.9) -> dict:
    return {"field": field, "value": value, "quote": quote, "revises": revises, "confidence": confidence}


# The claimant on policy 521585: one consistent claim, one near-miss that gets corrected,
# one hard conflict, one claim the records can't check.
SCRIPT = [
    ("Hi, I'm calling about policy 521585. I was driving my Saab when it happened.",
     [C("auto_make", "Saab", "I was driving my Saab")]),
    ("It was early, around six in the morning.",
     [C("incident_hour_of_the_day", "6", "around six in the morning", confidence=0.85)]),
    ("Five? Yes, you're right, it was five.",
     [C("incident_hour_of_the_day", "5", "you're right, it was five", revises=True)]),
    ("And it was a rear collision. Somebody went into the back of me.",
     [C("collision_type", "Rear Collision", "it was a rear collision")]),
    ("It was pouring with rain, too.",
     [C("other:weather", "raining", "It was pouring with rain")]),
]


class ScriptedClaimant:
    def __init__(self):
        self.i = 0

    def say(self, adjuster_last: str) -> str | None:
        if self.i >= len(SCRIPT):
            return None
        said = SCRIPT[self.i][0]
        self.i += 1
        return said


class ScriptedExtractor:
    """Looks the claimant's words up in the script. Free text in offline play files nothing."""

    def extract(self, said: str, last_answer: str = "") -> list[dict]:
        return next(([dict(c) for c in claims] for text, claims in SCRIPT if text == said), [])


class ScriptedAdjuster:
    """Same tools, same order as the live adjuster: recall -> inspect -> check_claims -> respond."""

    def __init__(self, tools, policy: str, memory_enabled: bool = True):
        self.tools = tools
        self.policy = policy
        self.memory_enabled = memory_enabled

    def run(self, said: str, history: list[dict]) -> dict:
        if not self.memory_enabled:
            return {"answer": "Thank you. I can't look anything up right now, so I can't verify that yet.",
                    "cited": []}
        ref = f"policy:{self.policy}"
        self.tools.run("recall", {"query": f"policy {self.policy} incident record", "scope": "records"})
        self.tools.run("inspect", {"ref": ref})
        self.tools.run("check_claims", {"record_ref": ref})
        lines = []
        for c in self.tools.checked or []:
            label = FIELDS.get(c["field"], {}).get("label", c["field"].split(":", 1)[-1])
            rec = c.get("record_value")
            if c["status"] == "consistent":
                lines.append(f"Thank you, that matches our record: {label} {rec}.")
            elif c["status"] == "near-miss":
                lines.append(f"Our record shows the {label} as {rec}, not {c['value']}. Can you confirm?")
            elif c["status"] == "conflict":
                lines.append(f"Our record shows a {label} of {rec}, not {c['value']}. Could you double-check that?")
            else:
                lines.append(f"Our records have nothing on the {label}, so I can't verify that.")
        if not lines:
            lines.append("Thank you. Go on.")
        return {"answer": " ".join(lines), "cited": [ref]}
