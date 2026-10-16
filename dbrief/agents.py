"""DBrief's agents on Claude: the agent, the claim extractor, and the autopilot speaker.

Every prompt is a template filled from the domain (persona, entity, speaker, field labels).
The agent's prompt contains no dataset values. Everything it knows about a record comes
through its tools, which are backed by memory (SurrealDB Agent Memory, or the local
fallback): recall, inspect, remember. It files what the speaker said with check_claims,
which runs the deterministic SurrealDB rules, and cites the record ids it recalled.
Whether a citation was actually retrieved is checked by the database.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .memory import env

AGENT_MODEL = os.environ.get("DBRIEF_AGENT_MODEL", "claude-sonnet-5-5")
EXTRACT_MODEL = os.environ.get("DBRIEF_EXTRACT_MODEL", "claude-haiku-4-5")
SPEAKER_MODEL = os.environ.get("DBRIEF_SPEAKER_MODEL", "claude-sonnet-5-5")


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


# ================================================================= AGENT =====
def agent_tools(d) -> list[dict]:
    ex = d.ref(d.demo_key or "123")
    return [
        _tool("recall", f"Search memory. scope 'records' searches the {d.display} records; scope 'conversation' "
                        f"searches what this {d.speaker} has told you before. Returns hits with record ids and a trace id.",
              {"query": {"type": "string"}, "scope": {"type": "string", "enum": ["records", "conversation"]}}),
        _tool("inspect", f"Read every field of one record you recalled, e.g. inspect('{ex}').",
              {"ref": {"type": "string", "description": f"a record id from recall, like {ex}"}}),
        _tool("check_claims", f"File what the {d.speaker} just said against one recalled {d.entity} record. SurrealDB "
                              "checks each claim with fixed rules and returns consistent / near-miss / conflict / "
                              "unverified with the record value. Call it once per turn, after inspect.",
              {"record_ref": {"type": "string"}}),
        _tool("remember", "Write a short note to your memory of this conversation.",
              {"text": {"type": "string"}}),
        _tool("respond", f"Reply to the {d.speaker}. Call exactly once, last.",
              {"answer": {"type": "string", "description": f"2-4 plain sentences to the {d.speaker}"},
               "cited_ids": {"type": "array", "items": {"type": "string"},
                             "description": "the record ids (from recall) this answer relies on"}}),
    ]


def agent_system(d) -> str:
    return f"""You are DBrief, {d.persona}.

You know nothing about any {d.entity} except what your tools return. Never state a fact about a {d.entity} that a
tool did not give you in this conversation.

Each turn:
1. recall the {d.speaker}'s {d.entity} from the records (scope "records"). Recall scope "conversation" only when the
   {d.speaker} refers to something they told you before that is not in the recent conversation shown to you.
2. inspect the {d.entity} record you recalled.
3. check_claims against that record. The database decides whether each claim matches; do not second-guess it.
4. respond, citing the record ids you recalled (e.g. {d.ref(d.demo_key or "123")}).

How to reply:
- Be courteous and plain. The {d.speaker} may simply be mistaken; never accuse them.
- consistent: acknowledge briefly.
- near-miss or conflict: say what the record shows, and ask the {d.speaker} to confirm or correct it.
- unverified: say the records have nothing on that point, so it can't be verified yet.
- 2-4 sentences. Always finish by calling the respond tool exactly once."""


class Agent:
    """Claude, with tools backed by memory. `tools` is the session's tool executor."""

    def __init__(self, domain, tools, memory_enabled: bool = True):
        self.client = _client()
        self.domain = domain
        self.tools = tools
        self.memory_enabled = memory_enabled

    def run(self, said: str, history: list[dict]) -> dict:
        self.llm_calls: list[tuple[str, float]] = []   # (Claude call, ms), for latency reports
        d = self.domain
        who = d.speaker.capitalize()
        context = "\n".join(f"{who}: {t['question']}\nYou: {t['answer']}" for t in history[-2:])
        prompt = (f"Recent conversation:\n{context}\n\n" if context else "") + f"The {d.speaker} says: \"{said}\""
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = agent_tools(d)
        system = agent_system(d)
        if not self.memory_enabled:
            tools = [t for t in tools if t["name"] == "respond"]
            system += "\n\nYour memory tools are switched off for this session. You cannot look anything up."
        for step in range(8):
            last = step == 7
            t0 = time.perf_counter()
            resp = _create(self.client, model=AGENT_MODEL, max_tokens=2000, system=system, messages=messages,
                           tools=[t for t in tools if t["name"] == "respond"] if last else tools)
            called = ",".join(b.name for b in resp.content if getattr(b, "type", "") == "tool_use") or "text"
            self.llm_calls.append((f"claude.agent step {step + 1} -> {called}", (time.perf_counter() - t0) * 1000))
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
    """Turns what the speaker said into typed claims. Sees field names and category values,
    never a record value. Its confidence travels with every claim."""

    def __init__(self, domain):
        self.client = _client()
        self.domain = domain

    def extract(self, said: str, last_answer: str = "") -> list[dict]:
        d = self.domain
        names = list(d.fields) + ["other"]
        field_lines = "\n".join(
            f"- {f}: {r['label']} ({r['type']})" + (f", one of: {', '.join(r['values'])}" if r["values"] else "")
            for f, r in d.fields.items())
        tool = _tool("file_claims", f"Every factual claim the {d.speaker} made about their {d.entity} record.", {
            "claims": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "required": ["field", "topic", "value", "quote", "revises", "confidence"],
                       "properties": {
                           "field": {"type": "string", "enum": names},
                           "topic": {"type": "string", "description": "for field 'other': a short snake_case topic, else ''"},
                           "value": {"type": "string"},
                           "quote": {"type": "string", "description": f"the exact words that make the claim"},
                           "revises": {"type": "boolean", "description": "true if they confirm a correction or change something they said"},
                           "confidence": {"type": "number", "description": "0-1: how clearly the words state this value"}}}}})
        system = f"""You extract factual claims from what a {d.speaker} says about their {d.entity} ({d.display}). Fields:
{field_lines}
- other: a concrete fact that no field above covers: set topic. Not the {d.entity} identifier, not a restatement
  of a field above, not feelings or plans.

Normalise values by type: hour = 0-23 ("around six in the morning" = 6, "half past five in the evening" = 17);
datetime = YYYY-MM-DD or YYYY-MM-DD HH:MM, only when the date is clear; number = plain digits ("about seventy
thousand dollars" = 70000); category = one of the listed values when the words clearly mean one.
Lower confidence for vague or hedged words. Never invent a claim; an empty list is fine.
If the agent just offered a value and the {d.speaker} agrees ("yes, that's right"), file that value with revises=true.
quote must be copied exactly from the {d.speaker}'s words; never file facts that only the agent stated.
Always reply by calling file_claims exactly once."""
        user = (f"The agent just said: \"{last_answer}\"\n" if last_answer else "") + f"The {d.speaker} says: \"{said}\""
        resp = _create(self.client, model=EXTRACT_MODEL, max_tokens=2000, system=system,
                       messages=[{"role": "user", "content": user}], tools=[tool])
        got = _tool_input(resp, "file_claims") or _json_from_text(_text(resp)) or {}
        out = []
        for c in got.get("claims", []) if isinstance(got, dict) else []:
            if c.get("field") not in names or not str(c.get("value", "")).strip():
                continue
            if c["field"] == "other":
                topic = (c.get("topic") or "note").lower()
                if d.entity in topic or d.key in topic:
                    continue  # the identifier picks the record; it is not a claim about it
                c["field"] = "other:" + topic
            if not c.get("quote") or c["quote"].strip().lower() not in said.lower():
                continue  # only what the speaker actually said, never facts echoed from the agent
            out.append(c)
        return out


# =============================================================== SPEAKER =====
class SpeakerAgent:
    """Autopilot: an LLM playing the real holder of one record from the dataset. Honest, with fuzzy memory."""

    def __init__(self, domain, row: dict):
        d = domain
        self.client = _client()
        self.domain = d
        facts = "\n".join(f"- {r['label']}: {row[f]}" for f, r in d.fields.items() if str(row.get(f, "")) not in ("", "?"))
        fuzz = d.autopilot.get("fuzz", "you are about an hour off on times, you round numbers, and you misremember one detail")
        self.system = f"""You are the {d.speaker} on {d.entity} {row[d.key]}, talking to DBrief ({d.display}).
What actually happened, as you remember it imperfectly:
{facts}

How you talk:
- You are honest, but your memory is fuzzy: {fuzz}.
- Give one or two facts per message, in plain spoken English, 1-3 sentences. Start by giving your {d.entity} number.
- When DBrief tells you what the record says, think about it: if it is close to what you remember, agree and
  confirm; if not, say you are fairly sure of your version.
- Mention one thing the records can't know."""
        self.messages: list[dict] = []

    def say(self, agent_last: str) -> str:
        self.messages.append({"role": "user", "content": agent_last or "Hello, this is DBrief. How can I help?"})
        resp = _create(self.client, model=SPEAKER_MODEL, max_tokens=400, system=self.system, messages=self.messages)
        text = _text(resp) or "That's all I remember."
        self.messages.append({"role": "assistant", "content": text})
        return text
