"""DBrief's agents on Claude: the adjuster, the claim extractor, and the autopilot claimant.

The adjuster's prompt contains no dataset values. Everything it knows about a policy
comes through its tools, which are backed by memory (SurrealDB Agent Memory, or the
local fallback): recall, inspect, remember. It files the claimant's claims with
check_claims, which runs the deterministic SurrealDB rules, and cites the record ids
it recalled. Whether a citation was actually retrieved is checked by the database.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .fields import FIELDS, VOCAB
from .memory import env

ADJUSTER_MODEL = os.environ.get("DBRIEF_ADJUSTER_MODEL", "claude-sonnet-5-5")
EXTRACT_MODEL = os.environ.get("DBRIEF_EXTRACT_MODEL", "claude-haiku-4-5")
CLAIMANT_MODEL = os.environ.get("DBRIEF_CLAIMANT_MODEL", "claude-sonnet-5-5")


def has_claude() -> bool:
    return bool(env("ANTHROPIC_API_KEY"))


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))


def _thinking_for(model: str) -> dict | None:
    """Sonnet 5.5 thinks before answering by default; `between_tools` turns that off for stage-speed turns.
    DBRIEF_THINKING=default leaves every model on its default."""
    if os.environ.get("DBRIEF_THINKING", "off") != "off":
        return None
    return {"type": "between_tools"} if "sonnet-5-5" in model else None


def _create(client, **kw):
    """messages.create without forced tool_choice (Claude 5.5 models reject "any"/"tool").
    If the thinking setting is refused, retry with the model's default."""
    import anthropic
    th = _thinking_for(kw["model"])
    if th:
        try:
            return client.messages.create(thinking=th, **kw)
        except anthropic.BadRequestError as e:
            if "thinking" not in str(e).lower() and "between_tools" not in str(e):
                raise
    return client.messages.create(**kw)


def _text(resp) -> str:
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()


def _json_from_text(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def _tool_input(resp, name: str) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == name:
            return dict(block.input)
    return None


def _tool(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": props,
                             "required": required if required is not None else list(props),
                             "additionalProperties": False}}


# ============================================================== ADJUSTER =====
ADJUSTER_TOOLS = [
    _tool("recall", "Search memory. scope 'records' searches the insurer's policy and incident records; "
                    "scope 'conversation' searches what this claimant has told us before. Returns hits with "
                    "record ids and a trace id.",
          {"query": {"type": "string"}, "scope": {"type": "string", "enum": ["records", "conversation"]}}),
    _tool("inspect", "Read every field of one record you recalled, e.g. inspect('policy:123456').",
          {"ref": {"type": "string", "description": "a record id from recall, like policy:123456"}}),
    _tool("check_claims", "File what the claimant just said against one recalled policy record. SurrealDB checks "
                          "each claim with fixed rules and returns consistent / near-miss / conflict / unverified "
                          "with the record value. Call it once per turn, after inspect.",
          {"record_ref": {"type": "string"}}),
    _tool("remember", "Write a short note to your memory of this conversation.",
          {"text": {"type": "string"}}),
    _tool("respond", "Reply to the claimant. Call exactly once, last.",
          {"answer": {"type": "string", "description": "2-4 plain sentences to the claimant"},
           "cited_ids": {"type": "array", "items": {"type": "string"},
                         "description": "the record ids (from recall) this answer relies on"}}),
]

ADJUSTER_SYSTEM = """You are DBrief, an insurance claims adjuster. A claimant is describing an incident to you.

You know nothing about any policy except what your tools return. Never state a fact about a policy that a tool
did not give you in this conversation.

Each turn:
1. recall the claimant's policy from the records (scope "records"). Recall scope "conversation" only when the
   claimant refers to something they told you before that is not in the recent conversation shown to you.
2. inspect the policy record you recalled.
3. check_claims against that record. The database decides whether each claim matches; do not second-guess it.
4. respond, citing the record ids you recalled (e.g. policy:123456).

How to reply:
- Be courteous and plain. The claimant may simply be mistaken; never accuse them.
- consistent: acknowledge briefly.
- near-miss or conflict: say what the record shows, and ask the claimant to confirm or correct it.
- unverified: say the records have nothing on that point, so it can't be verified yet.
- 2-4 sentences. Always finish by calling the respond tool exactly once."""


class Adjuster:
    """Claude, with tools backed by memory. `tools` is the session's tool executor."""

    def __init__(self, tools, memory_enabled: bool = True):
        self.client = _client()
        self.tools = tools
        self.memory_enabled = memory_enabled

    def run(self, said: str, history: list[dict]) -> dict:
        context = "\n".join(f"Claimant: {t['question']}\nYou: {t['answer']}" for t in history[-2:])
        prompt = (f"Recent conversation:\n{context}\n\n" if context else "") + f"The claimant says: \"{said}\""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        if self.memory_enabled:
            tools = ADJUSTER_TOOLS
            system = ADJUSTER_SYSTEM
        else:
            tools = [t for t in ADJUSTER_TOOLS if t["name"] == "respond"]
            system = ADJUSTER_SYSTEM + "\n\nYour memory tools are switched off for this session. You cannot look anything up."
        for step in range(8):
            last = step == 7
            resp = _create(self.client, model=ADJUSTER_MODEL, max_tokens=2000, system=system, messages=messages,
                           tools=[t for t in tools if t["name"] == "respond"] if last else tools)
            out = _tool_input(resp, "respond")
            if out:
                return {"answer": out.get("answer", ""), "cited": list(out.get("cited_ids") or [])}
            calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not calls:
                return {"answer": _text(resp) or "Let me look into that.", "cited": []}
            results = []
            for call in calls:
                body = self.tools.run(call.name, dict(call.input))
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": body})
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": results})
        return {"answer": "Let me look into that and come back to you.", "cited": []}


# ============================================================= EXTRACTOR =====
class Extractor:
    """Turns what the claimant said into typed claims. Sees field names and category vocabularies,
    never a record value. Its confidence travels with every claim."""

    def __init__(self):
        self.client = _client()

    def extract(self, said: str, last_answer: str = "") -> list[dict]:
        names = list(FIELDS) + ["other"]
        field_lines = "\n".join(
            f"- {f}: {r['label']}" + (f" (one of: {', '.join(VOCAB[f])})" if f in VOCAB else "")
            for f, r in FIELDS.items())
        tool = _tool("file_claims", "Every factual claim the claimant made about their policy or incident.", {
            "claims": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "required": ["field", "topic", "value", "quote", "revises", "confidence"],
                       "properties": {
                           "field": {"type": "string", "enum": names},
                           "topic": {"type": "string", "description": "for field 'other': a short snake_case topic, else ''"},
                           "value": {"type": "string"},
                           "quote": {"type": "string", "description": "the exact words that make the claim"},
                           "revises": {"type": "boolean", "description": "true if they confirm a correction or change something they said"},
                           "confidence": {"type": "number", "description": "0-1: how clearly the words state this value"}}}}})
        system = f"""You extract factual claims from what an insurance claimant says. Fields:
{field_lines}
- other: any other factual claim about the incident (weather, speed, ...): set topic.

Normalise values: hours as 0-23 ("around six in the morning" = 6, "half past five in the evening" = 17),
dates as YYYY-MM-DD only when the year is clear, amounts as plain numbers ("about seventy thousand dollars" = 70000),
yes/no fields as YES or NO, categories to the listed names when the words clearly mean one.
Lower confidence for vague or hedged words. Never invent a claim; an empty list is fine.
If the adjuster just offered a value and the claimant agrees ("yes, that's right"), file that value with revises=true.
quote must be copied exactly from the claimant's words; never file facts that only the adjuster stated. Always reply by calling file_claims exactly once."""
        user = (f"The adjuster just said: \"{last_answer}\"\n" if last_answer else "") + f"The claimant says: \"{said}\""
        resp = _create(self.client, model=EXTRACT_MODEL, max_tokens=2000, system=system,
                       messages=[{"role": "user", "content": user}], tools=[tool])
        got = _tool_input(resp, "file_claims") or _json_from_text(_text(resp)) or {}
        out = []
        for c in got.get("claims", []) if isinstance(got, dict) else []:
            if c.get("field") not in names or not str(c.get("value", "")).strip():
                continue
            if c["field"] == "other":
                c["field"] = "other:" + (c.get("topic") or "note")
            if not c.get("quote") or c["quote"].strip().lower() not in said.lower():
                continue  # only what the claimant actually said, never facts echoed from the adjuster
            out.append(c)
        return out


# ============================================================== CLAIMANT =====
class ClaimantAgent:
    """Autopilot: an LLM playing one real policyholder from the dataset. Honest, with fuzzy memory."""

    def __init__(self, row: dict):
        self.client = _client()
        facts = "\n".join(f"- {r['label']}: {row[f]}" for f, r in FIELDS.items() if row.get(f) not in (None, "", "?"))
        self.system = f"""You are the policyholder on insurance policy {row['policy_number']}, phoning your insurer about
your car incident. What actually happened (you remember it imperfectly):
{facts}

How you talk:
- You are honest, but your memory is fuzzy: you are about an hour off on the time, you round money to a nice
  number, and you believe it was a rear collision.
- Give one or two facts per message, in plain spoken English, 1-3 sentences. Start by giving your policy number.
- When the adjuster tells you what the record says, think about it: if it is close to what you remember, agree
  and confirm; if not, say you are fairly sure of your version.
- Mention one thing the records can't know (the weather, or what you were doing)."""
        self.messages: list[dict] = []

    def say(self, adjuster_last: str) -> str:
        self.messages.append({"role": "user", "content": adjuster_last or "Hello, DBrief claims. How can I help?"})
        resp = _create(self.client, model=CLAIMANT_MODEL, max_tokens=400, system=self.system, messages=self.messages)
        text = _text(resp) or "That's all I remember."
        self.messages.append({"role": "assistant", "content": text})
        return text
