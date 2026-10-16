"""One conversation: the speaker talks, memory recalls, the ledger checks.

One turn:
  speaker text ─► memory.remember (experiential, scope <domain scope>/<speaker>/<key>/<session>)
               ─► extractor: typed claims (field, value, exact quote, confidence)
               ─► agent tools: recall ─► inspect ─► check_claims
                       check_claims snapshots the record as memory returned it, then
                       fn::report files each claim ──► check_claim EVENT, same transaction
               ─► agent responds, citing record ids
                ─► trace row: traces used, ids retrieved, ids cited; uncited ids found by the database
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor



class Tools:
    """The agent's tools. Every fact about a record passes through memory here."""

    def __init__(self, session: "Session"):
        self.s = session
        self.reset()

    def reset(self) -> None:
        self.trace_ids: list[str] = []
        self.retrieved: list[str] = []
        self.recalls: list[dict] = []
        self.inspected: dict[str, dict] = {}
        self.checked: list[dict] | None = None
        self.memory_ms = 0.0

    def run(self, name: str, args: dict) -> str:
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            return f"unknown tool {name}"
        try:
            return fn(**args)
        except Exception as e:  # noqa: BLE001 - tool errors go back to the model
            return f"error: {e}"

    def t_recall(self, query: str, scope: str = "records") -> str:
        scopes = [self.s.scope] if scope == "conversation" else [self.s.domain.scope]
        r = self.s.memory.recall(query, scopes=scopes, k=8)
        self.memory_ms += r["ms"]
        self.recalls.append({**r, "query": query, "scope": scope})
        if r.get("trace_id"):
            self.trace_ids.append(r["trace_id"])
        for h in r["hits"]:
            if h.get("entity"):
                self.retrieved.append(h["entity"])
            if h.get("id"):
                self.retrieved.append(h["id"])
        lines = [f"- [{h.get('entity') or h['id']}] {h['text']}" for h in r["hits"]]
        return f"trace {r.get('trace_id')}\n" + ("\n".join(lines) or "no hits")

    def t_inspect(self, ref: str) -> str:
        if ":" not in ref:
            ref = self.s.domain.ref(ref)
        r = self.s.memory.inspect(ref)
        self.memory_ms += r["ms"]
        if not r["found"]:
            return f"{ref}: not found in memory"
        self.inspected[ref] = r
        return f"{ref}\n" + "\n".join(f"- {k} = {v}" for k, v in sorted(r["fields"].items()))

    def t_check_claims(self, record_ref: str) -> str:
        self.s.wait_for_claims()
        if ":" not in record_ref:
            record_ref = self.s.domain.ref(record_ref)
        if record_ref not in self.inspected:
            self.t_inspect(record_ref)
        rec = self.inspected.get(record_ref)
        if rec:
            tid = next((r["trace_id"] for r in reversed(self.recalls)
                        if any(h.get("entity") == record_ref for h in r["hits"])), None)
            self.s.ledger.snapshot(rec, tid, self.s.memory.backend)
        self.checked = self.s.file_claims(record_ref)
        if not self.checked:
            return f"no claims in what the {self.s.domain.speaker} just said"
        return "\n".join(f"- {c['field']}: {self.s.domain.speaker} said {c['value']!r} -> {c['status']}"
                         + (f" (record: {c['record_value']})" if c.get("record_value") is not None else "")
                         for c in self.checked)

    def t_remember(self, text: str) -> str:
        r = self.s.memory.remember(text, [self.s.scope])
        self.memory_ms += r["ms"]
        return "noted"


class Session:
    def __init__(self, domain, ledger, memory, agent_factory, extractor, key: str, memory_enabled: bool = True,
                 memory_note: str | None = None):
        self.domain = domain
        self.ledger = ledger
        self.memory = memory
        self.extractor = extractor
        self.key = key
        self.memory_enabled = memory_enabled
        self.memory_note = memory_note
        self.tools = Tools(self)
        self.agent = agent_factory(self.tools)
        self._pending: list[dict] = []
        self._extracting = None
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._turn = None
        self.history: list[dict] = []

    @property
    def scope(self) -> str:
        return self.domain.conversation_scope(self.key, self.ledger.session_id())

    @property
    def subject(self) -> str:
        return self.domain.ref(self.key)

    def wait_for_claims(self) -> None:
        """The extractor runs alongside the agent's lookups; claims are needed only at check time."""
        if self._extracting is not None:
            self._pending = self._extracting.result()
            self._extracting = None

    def file_claims(self, record_ref: str | None = None) -> list[dict]:
        """File this turn's pending claims; the check_claim event decides each one."""
        filed_ms = 0.0
        for c in self._pending:
            t0 = time.perf_counter()
            self.ledger.report(self._turn, {**c, "subject": record_ref or self.subject})
            filed_ms += (time.perf_counter() - t0) * 1000
        self._db_ms = filed_ms
        n_filed, self._pending = len(self._pending), []
        return self.ledger.claims_for_turn(int(self._turn.id)) if n_filed else []

    def ask(self, said: str) -> dict:
        self.tools.reset()
        self._db_ms = 0.0
        # Only this process's turns: after --resume, earlier turns are reachable only through memory.
        history = self.history[-2:]
        last_answer = history[-1]["answer"] if history else ""
        self._turn = turn = self.ledger.add_turn(said)
        n = int(turn.id)
        remembering = None
        if self.memory_enabled:
            d = self.domain
            note = (f"The {d.speaker} on {d.entity} {self.key} said (turn {n}): {said}", [self.scope])
            if self.memory.backend.startswith("SurrealDB Agent Memory"):  # hosted: written alongside the turn
                remembering = self._pool.submit(self.memory.remember, *note)
            else:  # the embedded store is used from one thread only
                self.tools.memory_ms += self.memory.remember(*note)["ms"]
        self._extracting = self._pool.submit(self.extractor.extract, said, last_answer)
        out = self.agent.run(said, history)
        self.wait_for_claims()
        if remembering is not None:
            remembering.result()
        if self._pending:  # the agent never called check_claims: file against nothing recalled
            self.file_claims(self.tools.inspected and next(iter(self.tools.inspected)) or None)
        self.ledger.set_answer(turn, out["answer"])
        self.history.append({"question": said, "answer": out["answer"]})
        tr = self.ledger.add_trace(turn, self.tools.trace_ids, self.tools.retrieved, out["cited"],
                                   self.memory.backend, self.tools.memory_ms)
        claims = self.ledger.claims_for_turn(n)
        flags = [f for f in self.ledger.flags_since(n) if f["turn"] == n]
        sup = [s for s in self.ledger.supersessions() if s["new_turn"] == n]
        return {"n": n, "said": said, "answer": out["answer"], "cited": out["cited"], "trace": tr,
                "recalls": self.tools.recalls, "inspected": list(self.tools.inspected),
                "claims": claims, "flags": flags, "superseded": sup, "review": self.ledger.review(),
                "memory_ms": self.tools.memory_ms, "backend": self.memory.backend, "memory_enabled": self.memory_enabled,
                "db": {"claims": len(claims), "edges": len(flags) + len(sup), "ms": self._db_ms}}
