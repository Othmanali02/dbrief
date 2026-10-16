"""Deterministic stand-ins for the LLMs. No network, no API calls.

Used for --offline play, for the stage fallback when the wifi dies, and as the regression test.
The words come from the domain's [[offline]] beats; memory is still real (local SurrealDB memory:
recall, inspect, traces), claims still go through fn::report and the check_claim event.
"""
from __future__ import annotations


class ScriptedSpeaker:
    def __init__(self, domain):
        self.beats = domain.offline
        self.i = 0

    def say(self, agent_last: str) -> str | None:
        if self.i >= len(self.beats):
            return None
        self.i += 1
        return self.beats[self.i - 1]["said"]


class ScriptedExtractor:
    """Looks the speaker's words up in the script. Free text in offline play files nothing."""

    def __init__(self, domain):
        self.beats = domain.offline

    def extract(self, said: str, last_answer: str = "") -> list[dict]:
        beat = next((b for b in self.beats if b["said"] == said), None)
        return [{"revises": False, "confidence": 0.9, **c} for c in (beat or {}).get("claims", [])]


class ScriptedAgent:
    """Same tools, same order as the live agent: recall -> inspect -> check_claims -> respond."""

    def __init__(self, domain, tools, key: str, memory_enabled: bool = True):
        self.d = domain
        self.tools = tools
        self.key = key
        self.memory_enabled = memory_enabled

    def run(self, said: str, history: list[dict]) -> dict:
        if not self.memory_enabled:
            return {"answer": "Thank you. I can't look anything up right now, so I can't verify that yet.", "cited": []}
        if not any(b["said"] == said for b in self.d.offline):
            return self._from_conversation(said)
        ref = self.d.ref(self.key)
        self.tools.run("recall", {"query": f"{self.d.entity} {self.key}", "scope": "records"})
        self.tools.run("inspect", {"ref": ref})
        self.tools.run("check_claims", {"record_ref": ref})
        lines = []
        for c in self.tools.checked or []:
            label, rec = self.d.label(c["field"]), c.get("record_value")
            if c["status"] == "consistent":
                lines.append(f"Thank you, that matches our record: {label} {rec}.")
            elif c["status"] == "near-miss":
                lines.append(f"Our record shows the {label} as {rec}, not {c['value']}. Can you confirm?")
            elif c["status"] == "conflict":
                lines.append(f"Our record shows the {label} as {rec}, not {c['value']}. Could you double-check that?")
            else:
                lines.append(f"Our records have nothing on the {label}, so I can't verify that.")
        return {"answer": " ".join(lines) or "Thank you. Go on.", "cited": [ref]}

    def _from_conversation(self, said: str) -> dict:
        """Off-script: answer from what memory holds about this conversation (used by --resume)."""
        self.tools.run("recall", {"query": said, "scope": "conversation"})
        hits = (self.tools.recalls[-1]["hits"] if self.tools.recalls else [])[:3]
        if not hits:
            return {"answer": "I have nothing on record from earlier in our conversation.", "cited": []}
        return {"answer": "From earlier in our conversation, memory has: " + " / ".join(h["text"] for h in hits),
                "cited": [h["id"] for h in hits]}
