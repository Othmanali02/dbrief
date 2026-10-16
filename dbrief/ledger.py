"""The claim ledger: what the speaker said, checked against what memory returned. Embedded SurrealDB."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from datetime import datetime, timezone

from surrealdb import RecordID, Surreal



def rid(table: str, key: Any) -> RecordID:
    return RecordID(table, key)


def as_str(v: Any) -> str:
    """Render a RecordID or anything else as 'table:key'."""
    if isinstance(v, RecordID):
        return f"{v.table_name}:{v.id}"
    return str(v)


def hhmm(v: Any) -> str:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%H:%M")
    s = str(v)
    return s[11:16] if len(s) >= 16 else s

SCHEMA = (Path(__file__).parent / "ledger.surql").read_text()
RESET_TABLES = ["contradicts", "supersedes", "claim", "trace", "turn", "record", "meta"]


class Ledger:
    def __init__(self, domain, url: str | None = None, fresh: bool = True):
        self.domain = domain
        self.url = url or os.environ.get("DBRIEF_DB_URL", "surrealkv+versioned://.dbrief-db")
        if fresh and self.url.startswith("surrealkv"):
            path = self.url.split("://", 1)[1].split("?", 1)[0]
            if path and Path(path).exists():
                shutil.rmtree(path)
        self.db = Surreal(self.url)
        self.remote = not self.url.startswith(("mem://", "surrealkv", "file://"))
        if self.remote:
            self.db.signin({"username": os.environ.get("DBRIEF_DB_USER", "root"),
                            "password": os.environ.get("DBRIEF_DB_PASS", "root")})
        self.db.use("dbrief", "ledger")
        self.queries: list[str] = []   # what we ran, for the /sql panel
        if fresh:
            self.db.query(SCHEMA)
            if self.remote:  # keep the database so live queries in Studio survive a new session
                self.db.query("".join(f"DELETE {t};" for t in RESET_TABLES))
            self.db.query("DELETE field_rule; DELETE alias;")
            self.db.query("INSERT INTO field_rule $rows;", {"rows": [
                {"field": f, "type": r["type"], "label": r["label"],
                 **{k: r[k] for k in ("window_hours", "pct", "abs") if r.get(k) is not None}}
                for f, r in domain.fields.items()]})
            if domain.aliases:
                self.db.query("INSERT INTO alias $rows;", {"rows": [
                    {"word": w, "canonical": c} for w, c in domain.aliases.items()]})

    def q(self, sql: str, params: dict | None = None, show: bool = False):
        if show:
            self.queries.append(sql.strip())
        return self.db.query(sql, params or {})

    def session_id(self) -> str:
        """One id per conversation, kept in the ledger so --resume continues the same memory scope."""
        if not getattr(self, "_sid", None):
            rows = self.q("SELECT VALUE sid FROM meta WHERE name = 'session';")
            if rows:
                self._sid = rows[0]
            else:
                import uuid
                self._sid = "s" + uuid.uuid4().hex[:8]
                self.q("CREATE meta SET name = 'session', sid = $sid;", {"sid": self._sid})
        return self._sid

    # ---------------------------------------------------------------- records
    def snapshot(self, inspected: dict, trace_id: str | None, source: str) -> None:
        """Store a record exactly as memory returned it. The check reads only this."""
        self.q("""UPSERT record SET ref = $ref, fields = $fields, attr_ids = $ids, source = $src,
                         trace_id = $tid, at = time::now() WHERE ref = $ref;""",
               {"ref": inspected["ref"], "fields": inspected["fields"], "ids": inspected.get("attr_ids", {}),
                "src": source, "tid": trace_id})
        if not self.q("SELECT id FROM record WHERE ref = $ref;", {"ref": inspected["ref"]}):
            self.q("CREATE record SET ref = $ref, fields = $fields, attr_ids = $ids, source = $src, trace_id = $tid;",
                   {"ref": inspected["ref"], "fields": inspected["fields"], "ids": inspected.get("attr_ids", {}),
                    "src": source, "tid": trace_id})

    def record(self, ref: str) -> dict | None:
        rows = self.q("SELECT * FROM record WHERE ref = $ref;", {"ref": ref})
        return rows[0] if rows else None

    # ------------------------------------------------------------------ turns
    def turn_count(self) -> int:
        return int(self.q("RETURN count((SELECT id FROM turn));") or 0)

    def add_turn(self, said: str, answer: str = "") -> RecordID:
        n = self.turn_count() + 1
        self.q("CREATE $id SET n = $n, question = $q, answer = $a;",
               {"id": rid("turn", n), "n": n, "q": said, "a": answer})
        return rid("turn", n)

    def set_answer(self, turn: RecordID, answer: str) -> None:
        self.q("UPDATE $id SET answer = $a;", {"id": turn, "a": answer})

    def recent_turns(self, k: int) -> list[dict]:
        rows = self.q("SELECT n, question, answer FROM turn ORDER BY n DESC LIMIT $k;", {"k": k})
        return list(reversed(rows or []))

    def turn_time(self, n: int):
        r = self.q("SELECT VALUE at FROM $id;", {"id": rid("turn", n)})
        return r[0] if r else None

    def now(self):
        return self.q("RETURN time::now();")

    # ----------------------------------------------------------------- claims
    def report(self, turn: RecordID, claim: dict) -> RecordID:
        """File one claim through fn::report. The check_claim event fires inside this call."""
        return self.q("RETURN fn::report($turn, $c);", {"turn": turn, "c": {
            "subject": claim["subject"], "field": claim["field"], "value": str(claim["value"]),
            "quote": claim.get("quote", ""), "revises": bool(claim.get("revises", False)),
            "confidence": float(claim.get("confidence", 1.0)), "trace_id": claim.get("trace_id"),
        }}, show=True)

    def claims(self, live_only: bool = False) -> list[dict]:
        where = "WHERE retracted = false" if live_only else ""
        return self.q(f"""SELECT id, subject, field, value, quote, status, record_value, retracted, confidence,
                                 trace_id, turn.n AS turn, count(->contradicts) AS hits
                          FROM claim {where} ORDER BY turn, field;""") or []

    def claims_for_turn(self, n: int) -> list[dict]:
        return [c for c in self.claims() if c["turn"] == n]

    def flags_since(self, turn_n: int) -> list[dict]:
        return self.q("""
            SELECT id, kind, status, certainty, field, claimed, expected, reason, trace_id,
                   in.quote AS quote, in.turn.n AS turn, in.id AS claim,
                   out AS against, out.ref AS ref, out.quote AS other_quote, out.turn.n AS other_turn
            FROM contradicts WHERE in.turn.n >= $n ORDER BY turn, kind;""", {"n": turn_n}, show=True) or []

    def all_flags(self) -> list[dict]:
        return self.flags_since(0)

    def supersessions(self) -> list[dict]:
        return self.q("""SELECT in.quote AS new, in.value AS new_value, in.status AS new_status, in.turn.n AS new_turn,
                                in.field AS field, out.quote AS old, out.value AS old_value, out.turn.n AS old_turn, at
                         FROM supersedes ORDER BY at;""") or []

    def review(self) -> dict:
        return self.q("RETURN fn::review();") or {}

    def blame(self, turn_n: int) -> list[dict]:
        return self.q("""
            SELECT id, field, value, quote, status, record_value, confidence, retracted, said_at, trace_id,
                   turn.n AS turn, turn.question AS question,
                   ->contradicts.{kind, status, certainty, reason} AS flagged_by,
                   <-supersedes<-claim.{quote, value, turn: turn.n} AS replaced_by,
                   ->supersedes->claim.{quote, value, turn: turn.n} AS replaces
            FROM claim WHERE turn.n = $n ORDER BY field;""", {"n": turn_n}, show=True) or []

    def story_at(self, when: Any) -> list[dict]:
        """System time: the speaker's live story exactly as the ledger held it at `when`."""
        rows = self.q("""SELECT id, field, value, status, turn.n AS turn FROM claim
                         WHERE retracted = false ORDER BY field VERSION <datetime> $t;""",
                      {"t": when}, show=True) or []
        exists = {as_str(i) for i in self.q("SELECT VALUE id FROM claim;") or []}
        return [r for r in rows if as_str(r["id"]) in exists]

    # ------------------------------------------------------------------ trace
    def add_trace(self, turn: RecordID, trace_ids: list[str], retrieved: list[str], cited: list[str],
                  backend: str, memory_ms: float) -> dict:
        self.q("""CREATE trace SET turn = $turn, trace_ids = $tids, retrieved = $ret, cited = $cited,
                                   backend = $b, memory_ms = $ms;""",
               {"turn": turn, "tids": trace_ids, "ret": sorted(set(retrieved)), "cited": cited,
                "b": backend, "ms": float(memory_ms)}, show=True)
        return self.trace_for(int(turn.id))

    def trace_for(self, n: int) -> dict | None:
        rows = self.q("SELECT * FROM trace WHERE turn = $t;", {"t": rid("turn", n)})
        return rows[0] if rows else None
