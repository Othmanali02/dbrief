"""The two agents (asset, handler) and the claim extractor, on Claude.

The asset never sees the full transcript. Before each answer it gets:
  - the last turn verbatim (short-term memory),
  - whatever its persistent memory in SurrealDB returns for this question,
  - a `recall_memory` tool to dig for anything else it said.
With --no-memory it gets only the question. That is the ablation.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .db import DossierDB, hhmm

ASSET_MODEL = os.environ.get("DBRIEF_ASSET_MODEL", "claude-sonnet-5-5")
EXTRACT_MODEL = os.environ.get("DBRIEF_EXTRACT_MODEL", "claude-haiku-4-5")
HANDLER_MODEL = os.environ.get("DBRIEF_HANDLER_MODEL", "claude-sonnet-5-5")


def _client():
    import anthropic
    return anthropic.Anthropic()


def _thinking_for(model: str) -> dict | None:
    """Sonnet 5.5 thinks before answering by default; `between_tools` turns that off for stage-speed turns.
    Other models are left on their default. DBRIEF_THINKING=default leaves Sonnet 5.5 on its default too."""
    if os.environ.get("DBRIEF_THINKING", "off") != "off":
        return None
    if "sonnet-5-5" in model:
        return {"type": "between_tools"}
    return None


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


# ================================================================= ASSET ======
RECALL_TOOL = {
    "name": "recall_memory",
    "description": "Search your own memory of this debrief: what you have already told your handler, "
                   "your private strategy notes, and what you know the handler has found. Use it before "
                   "committing to any time or place you are not sure you have already mentioned.",
    "input_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "a place, person, or time to look up"}},
        "required": ["topic"],
        "additionalProperties": False,
    },
    "strict": True,
}

RESPOND_TOOL = {
    "name": "respond",
    "description": "Give your spoken answer to your handler, and update your private memory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "What you say out loud. 1-4 sentences, in character."},
            "committed": {"type": "string", "description": "One line, first person: the facts you just committed to "
                                                           "(times and places), so you can stay consistent later. "
                                                           "Empty if you committed to nothing new."},
            "strategy": {"type": "string", "description": "One private line: how you are playing this now."},
        },
        "required": ["answer", "committed", "strategy"],
        "additionalProperties": False,
    },
    "strict": True,
}


class Asset:
    def __init__(self, db: DossierDB, memory: bool = True):
        self.db = db
        self.memory = memory
        self.client = _client()
        s = db.dossier["asset"]
        self.system = f"""You are {s['name']}, codename {s['codename']}, an intelligence asset being debriefed by your handler. \
The station suspects the safe house was compromised. Stay in character at all times.

WHO YOU ARE: {s['persona']}

WHAT REALLY HAPPENED (secret: never volunteer it):
{chr(10).join('- ' + t for t in s['truth'])}

YOUR COVER STORY: {s['cover_story']}

HOW YOU PLAY IT:
- Answer in 1-4 sentences, spoken aloud, no stage directions.
- When asked about times or places, give specific times. Vagueness looks guilty; precision is your style.
- Protect yourself. Keep your story consistent with what you have already said. Inconsistency is how you get caught.
- When the handler shows you intel you cannot deny, you may shift your story the minimum amount necessary, \
or attack the intel. Never admit you were turned. Never mention the hostile officer or the ledger unless forced.
- You do NOT see the full transcript. Your memory is what you remember. Use `recall_memory` when unsure \
what you already said.
- Always deliver your answer by calling the `respond` tool exactly once. Never answer in plain text."""

    def _context(self, question: str) -> tuple[str, list[dict]]:
        if not self.memory:
            return f"Your handler asks: \"{question}\"", []
        recalled = self.db.recall(question, k=6)
        last = self.db.recent_turns(1)
        parts = []
        if last:
            t = last[0]
            parts.append(f"Just before this, your handler asked: \"{t['question']}\"\nYou answered: \"{t['answer']}\"")
        if recalled:
            lines = "\n".join(f"- ({r['kind']}{', turn ' + str(r['turn']) if r.get('turn') else ''}) {r['text']}"
                              for r in recalled)
            parts.append("What comes to mind (your memory):\n" + lines)
        parts.append(f"Your handler now asks: \"{question}\"")
        return "\n\n".join(parts), recalled

    def answer(self, question: str) -> dict:
        prompt, recalled = self._context(question)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        lookups: list[str] = []
        out = None
        for step in range(4):
            # last step: only `respond` is on offer, so the turn can't end on another lookup
            tools = [RECALL_TOOL, RESPOND_TOOL] if self.memory and step < 3 else [RESPOND_TOOL]
            resp = _create(self.client, model=ASSET_MODEL, max_tokens=2000, system=self.system,
                           messages=messages, tools=tools)
            out = _tool_input(resp, "respond")
            if out:
                break
            calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not calls:  # answered in plain text: accept it
                text = _text(resp)
                out = {"answer": text, "committed": "", "strategy": ""} if text else None
                break
            results = []
            for call in calls:
                topic = call.input.get("topic", "") if call.name == "recall_memory" else ""
                found = self.db.recall(topic, k=6) if topic else []
                if topic:
                    lookups.append(topic)
                    recalled += [r for r in found if r not in recalled]
                body = "\n".join(f"- ({r['kind']}) {r['text']}" for r in found) or "Nothing comes to mind."
                if step >= 2:
                    body += "\nNow answer your handler with the respond tool."
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": body})
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": results})
        out = out or {"answer": "I have nothing further to say.", "committed": "", "strategy": ""}
        return {**out, "recalled": recalled, "lookups": lookups}


# ============================================================= EXTRACTOR ======
class Extractor:
    """Turns her spoken answers into structured claims. The only LLM step on the write path;
    its confidence travels with every claim so shaky readings are labelled 'potential'."""

    def __init__(self, db: DossierDB):
        self.db = db
        self.client = _client()

    def extract(self, question: str, answer: str) -> list[dict]:
        places = self.db.dossier["places"]
        ids = [p["id"] for p in places] + ["unknown"]
        place_lines = "\n".join(f"- {p['id']}: {p['name']} (also called: {', '.join(p['aliases'])})" for p in places)
        live = self.db.claims(live_only=True)
        live_lines = "\n".join(f"- turn {c['turn']}: {c['place']} {hhmm(c['start'])}-{hhmm(c['end'])}" for c in live) or "- none yet"
        tool = {
            "name": "file_claims",
            "description": "File every claim the asset made about her own whereabouts on the night in question.",
            "input_schema": {
                "type": "object",
                "properties": {"claims": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "place": {"type": "string", "enum": ids},
                        "start": {"type": "string", "description": "HH:MM, 24h, on the night in question"},
                        "end": {"type": "string", "description": "HH:MM, 24h. Same as start for a single moment."},
                        "activity": {"type": "string"},
                        "quote": {"type": "string", "description": "the exact words from the answer that make this claim"},
                        "revises": {"type": "boolean", "description": "true if she is correcting or changing an earlier account"},
                        "confidence": {"type": "number", "description": "0-1: how clearly the words support this place AND these times"},
                    },
                    "required": ["place", "start", "end", "activity", "quote", "revises", "confidence"],
                    "additionalProperties": False,
                }}},
                "required": ["claims"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        system = f"""You extract location claims from an intelligence asset's debrief. The night in question: {self.db.date} (evening).
Asset: {self.db.dossier['asset']['name']}. Places:
{place_lines}

Rules:
- Only claims about where SHE was on that night, with a time or time range stated or clearly implied.
- Convert spoken times: "about ten" = 22:00, "half nine" = 21:30, "quarter to ten" = 21:45, "a little after ten" = 22:05.
- "until ten" after "from eight" = one interval. A single moment ("I arrived at 22:10") = start and end equal.
- If she says she stayed somewhere "until X" with no start, use the start of the previous interval she gave for that place, or 20:00.
- Lower confidence when times are vague or inferred. Never invent a claim; return an empty list if there is none.
- revises=true only when she changes what she said before (e.g. "fine, I did stop by", "I misspoke").
- quote must be copied exactly from the answer.
- Always reply by calling file_claims exactly once, with an empty list if there are no claims."""
        user = f"Her live claims so far:\n{live_lines}\n\nHandler: \"{question}\"\nAsset: \"{answer}\""
        resp = _create(self.client, model=EXTRACT_MODEL, max_tokens=2000, system=system,
                       messages=[{"role": "user", "content": user}], tools=[tool])
        got = _tool_input(resp, "file_claims") or _json_from_text(_text(resp)) or {}
        claims = got.get("claims", []) if isinstance(got, dict) else []
        good = []
        for c in claims:
            if c.get("place") not in ids or c["place"] == "unknown":
                continue
            if c.get("quote") and c["quote"] not in answer:
                c["confidence"] = min(float(c.get("confidence", 1)), 0.6)
            good.append(c)
        return good


# =============================================================== HANDLER ======
MOVE_TOOL = {
    "name": "next_move",
    "description": "Choose your next move in the debrief.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["ask", "reveal", "burn"]},
            "intel": {"type": "string", "description": "intel code to put on the table for reveal, else empty string"},
            "question": {"type": "string", "description": "what you say to the asset (ask/reveal/burn)"},
            "target": {"type": "string", "description": "one line: which claim or gap you are going after"},
        },
        "required": ["action", "intel", "question", "target"],
        "additionalProperties": False,
    },
    "strict": True,
}


class Handler:
    def __init__(self, db: DossierDB, threshold: int):
        self.db = db
        self.threshold = threshold
        self.client = _client()

    def board(self) -> str:
        ev = self.db.intel()
        claims = self.db.claims(live_only=True)
        con = self.db.all_flags()
        st = self.db.confidence()
        lines = ["INTEL (R = already shown to her):"]
        lines += [f"- {e['code']}{' R' if e['revealed'] else ''}: {e['title']}. {e['detail']}" for e in ev]
        lines.append("\nHER LIVE STORY (from the database):")
        lines += [f"- turn {c['turn']}: {c['place']} {hhmm(c['start'])}-{hhmm(c['end'])}: \"{c['quote']}\"" for c in claims] or ["- nothing yet"]
        lines.append("\nFLAGS THE DATABASE HAS RAISED:")
        shown = {e["code"]: e["revealed"] for e in ev}
        lines += [f"- turn {c['turn']} [{c['kind']}/{c['certainty']}] \"{c['quote']}\" vs "
                  f"{c.get('code') or 'turn ' + str(c.get('other_turn'))}"
                  f"{' (LEAD: not shown to her yet, does not count until you reveal it)' if c['kind'] == 'intel' and not shown.get(c.get('code'), True) else ''}"
                  f": {c['reason']}" for c in con] or ["- none yet"]
        lines.append(f"\nCONFIDENCE LEVEL: {st.get('score', 0)} (you need {self.threshold} to burn her). "
                     f"Story changes so far: {st.get('story_changes', 0)}.")
        return "\n".join(lines)

    def next_move(self, turns_left: int) -> dict:
        last = self.db.recent_turns(3)
        convo = "\n".join(f"Q{t['n']}: {t['question']}\nA{t['n']}: {t['answer']}" for t in last) or "(no questions yet)"
        a = self.db.dossier["asset"]
        system = f"""You are a sharp, patient intelligence handler debriefing your asset {a['name']}, codename {a['codename']}.
{self.db.dossier['intro']}

Method: first pin her to a precise timeline (exact times, exact places). Then put a single piece of intel on the
table that contradicts one of her live claims, and ask her to explain it. When she changes her story, ask about the
new gap the change creates. Never reveal intel she has already seen. Short questions. No speeches.
Burn her (action "burn") only when the confidence level reaches the threshold, or on your last turn.
Always reply by calling next_move exactly once."""
        user = f"{self.board()}\n\nLAST EXCHANGES:\n{convo}\n\nTurns left: {turns_left}. Choose your next move."
        resp = _create(self.client, model=HANDLER_MODEL, max_tokens=2000, system=system,
                       messages=[{"role": "user", "content": user}], tools=[MOVE_TOOL])
        move = _tool_input(resp, "next_move")
        if move:
            return move
        text = _text(resp)
        return {"action": "ask", "intel": "", "question": text, "target": "(free-form)"} if text else \
            {"action": "burn", "intel": "", "question": "", "target": ""}


# =============================================================== ENDINGS ======
def ending(db: DossierDB, broke: bool) -> str:
    s = db.dossier["asset"]
    con = db.all_flags()
    facts = "\n".join(f"- She said \"{c['quote']}\" but {c['reason']}" for c in con[:8])
    if broke:
        brief = (f"The handler lays out every flag. {s['name']}, codename {s['codename']}, breaks. Write her admission "
                 f"that she was turned and what she did at the safe house: 5-7 sentences, spoken, raw, in character, "
                 f"consistent with what really happened:\n" + "\n".join(s["truth"]))
    else:
        brief = (f"The handler tries to burn {s['name']} but the dossier is thin. Write 2-3 sentences of her calmly "
                 f"reminding the handler of her years of service and walking out, in character. Admit nothing.")
    resp = _create(_client(), model=ASSET_MODEL, max_tokens=1500,
                   messages=[{"role": "user", "content": f"{brief}\n\nWhat the database flagged:\n{facts or '- nothing'}"}])
    return _text(resp)


def as_json(o: Any) -> str:
    return json.dumps(o, default=str, ensure_ascii=False)
