"""SurrealDB layer: the dossier, the debrief record, the asset's memory, and the flags."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from surrealdb import RecordID, Surreal

HERE = Path(__file__).parent
SCHEMA = (HERE / "schema.surql").read_text()

# Full-text index syntax differs between SurrealDB 2.x and 3.x.
FT_INDEXES = [
    ("claim_quote_ft", "claim", "quote"),
    ("memory_text_ft", "memory", "text"),
]


RESET_TABLES = ["discrepancy", "supersedes", "claim", "memory", "turn", "intel", "place"]


def rid(table: str, key: str) -> RecordID:
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


class DossierDB:
    def __init__(self, url: str | None = None, fresh: bool = True, embedder=None):
        self.url = url or os.environ.get("DBRIEF_DB_URL", "surrealkv+versioned://.dbrief-db")
        if fresh and self.url.startswith("surrealkv"):
            path = self.url.split("://", 1)[1].split("?", 1)[0]
            if path and Path(path).exists():
                import shutil
                shutil.rmtree(path)
        self.db = Surreal(self.url)
        if not self.url.startswith(("mem://", "surrealkv", "file://")):
            self.db.signin({
                "username": os.environ.get("DBRIEF_DB_USER", "root"),
                "password": os.environ.get("DBRIEF_DB_PASS", "root"),
            })
        self.db.use("dbrief", "dossier")
        self.embedder = embedder
        self.dossier: dict = {}
        self.date = ""
        self.subject = ""
        self.queries: list[str] = []   # what we ran, for the /sql panel
        if fresh:
            self.db.query(SCHEMA)
            self._define_fulltext()
            if not self.url.startswith(("surrealkv", "mem://", "file://")):
                # remote server: clear the rows but keep the database, so live queries open in Studio survive
                self.db.query("".join(f"DELETE {t};" for t in RESET_TABLES))

    # ------------------------------------------------------------------ setup
    def _define_fulltext(self) -> None:
        for name, table, field in FT_INDEXES:
            v3 = f"DEFINE INDEX OVERWRITE {name} ON {table} FIELDS {field} FULLTEXT ANALYZER statements BM25;"
            v2 = f"DEFINE INDEX OVERWRITE {name} ON {table} FIELDS {field} SEARCH ANALYZER statements BM25;"
            try:
                self.db.query(v3)
            except Exception:  # noqa: BLE001 - older engine
                self.db.query(v2)

    def q(self, sql: str, params: dict | None = None, show: bool = False):
        if show:
            self.queries.append(sql.strip())
        return self.db.query(sql, params or {})

    def ts(self, hm: str) -> str:
        """'21:22' on the dossier date -> ISO datetime string (dossier times are UTC-naive)."""
        h, m = (int(x) for x in hm.strip().split(":"))
        day = date.fromisoformat(self.date)
        if h < 6:  # 00:30 belongs to the night after the dossier date
            day += timedelta(days=1)
        return f"{day.isoformat()}T{h:02d}:{m:02d}:00Z"

    def attach_dossier(self, path: Path) -> dict:
        """Resume: read the dossier file, but trust the database for everything that happened."""
        dossier = json.loads(Path(path).read_text())
        self.dossier, self.date, self.subject = dossier, dossier["date"], dossier["asset"]["id"]
        return dossier

    def load_dossier(self, path: Path) -> dict:
        dossier = self.attach_dossier(path)
        for p in dossier["places"]:
            self.q("CREATE $id SET name = $name, aliases = $aliases;",
                   {"id": rid("place", p["id"]), "name": p["name"], "aliases": p["aliases"]})
        for e in dossier["intel"]:
            self.q("""CREATE $id CONTENT {
                        code: $code, title: $title, detail: $detail, kind: $kind,
                        subject: $subject, source: $source, trust: $trust,
                        revealed: $revealed,
                        place: $place,
                        start: IF $start THEN <datetime> $start ELSE NONE END,
                        end:   IF $end   THEN <datetime> $end   ELSE NONE END
                    };""", {
                "id": rid("intel", e["code"]), "code": e["code"], "title": e["title"], "detail": e["detail"], "kind": e["kind"],
                "subject": self.subject, "source": e["source"], "trust": float(e["trust"]),
                "revealed": bool(e.get("known_at_start", False)),
                "place": rid("place", e["place"]) if e.get("place") else None,
                "start": self.ts(e["start"]) if e.get("start") else None,
                "end": self.ts(e["end"]) if e.get("end") else None,
            })
        return dossier

    # ---------------------------------------------------------------- reading
    def places(self) -> list[dict]:
        return self.q("SELECT * FROM place ORDER BY name;")

    def intel(self, only_revealed: bool = False) -> list[dict]:
        where = "WHERE revealed = true" if only_revealed else ""
        return self.q(f"SELECT *, place.name AS place_name FROM intel {where} ORDER BY code;")

    def reveal(self, code: str) -> dict | None:
        rows = self.q("UPDATE $id SET revealed = true RETURN AFTER;",
                      {"id": rid("intel", code.upper())})
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        return row

    def turn_count(self) -> int:
        r = self.q("RETURN count((SELECT id FROM turn));")
        return int(r or 0)

    def recent_turns(self, k: int) -> list[dict]:
        rows = self.q("SELECT n, question, answer FROM turn ORDER BY n DESC LIMIT $k;", {"k": k})
        return list(reversed(rows or []))

    # ---------------------------------------------------------------- writing
    def add_turn(self, question: str, answer: str) -> RecordID:
        n = self.turn_count() + 1
        self.q("CREATE $id SET n = $n, question = $q, answer = $a;",
               {"id": rid("turn", n), "n": n, "q": question, "a": answer})
        return rid("turn", n)

    def report(self, turn: RecordID, claim: dict) -> RecordID:
        """File one claim through fn::report. The raise_flags event fires inside this call."""
        start, end = claim["start"], claim["end"]
        if end <= start:  # a moment, not a stay: give it ten minutes
            h, m = map(int, start.split(":"))
            m += 10
            end = f"{h + m // 60:02d}:{m % 60:02d}"
        payload = {
            "subject": self.subject,
            "place": rid("place", claim["place"]),
            "start": self.ts(start),
            "end": self.ts(end),
            "activity": claim.get("activity", ""),
            "quote": claim["quote"],
            "revises": bool(claim.get("revises", False)),
            "confidence": float(claim.get("confidence", 1.0)),
            "embedding": self.embedder(claim["quote"]) if self.embedder else None,
        }
        return self.q("RETURN fn::report($turn, $c);", {"turn": turn, "c": payload}, show=True)

    def remember(self, kind: str, text: str, turn: RecordID | None = None) -> None:
        self.q("CREATE memory SET kind = $k, text = $t, turn = $turn, embedding = $e;", {
            "k": kind, "t": text, "turn": turn,
            "e": self.embedder(text) if self.embedder else None,
        })

    # -------------------------------------------------------- asset's recall
    STOP = set("""a an the and or but of to in on at for from with by about what where when who why how
        was were is are be been did do does you your yours i me my we our they them it its that this
        there then than so just very after before into out up down over again tell said say says
        ms mr mrs miss please now ok okay well night evening tuesday all any some still really celeste""".split())

    def _terms(self, query: str, cap: int = 8) -> list[str]:
        """Words worth searching for, plus the canonical name of any place she calls by a nickname."""
        import re
        low = query.lower()
        words = [w for w in re.findall(r"[a-zà-ÿ0-9']+", low) if len(w) > 2 and w not in self.STOP]
        out: list[str] = []
        for p in self.dossier.get("places", []):        # "the restaurant" -> Lumière
            names = [p["name"].lower(), *p["aliases"]]
            if any(n in low for n in names):
                out.append(p["name"])
        for w in words:
            if w not in out:
                out.append(w)
        return out[:cap]

    def recall(self, query: str, k: int = 6) -> list[dict]:
        """What the asset remembers about `query`: hybrid of BM25 and (optional) vectors."""
        hits: dict[str, dict] = {}
        terms = self._terms(query)
        if terms:
            where = " OR ".join(f"text @{i + 1}@ $t{i}" for i in range(len(terms)))
            params = {f"t{i}": t for i, t in enumerate(terms)}
            try:
                rows = self.q(f"""SELECT id, kind, text, turn.n AS turn FROM memory
                                  WHERE {where} ORDER BY turn DESC LIMIT {int(k)};""", params, show=True)
                for r in rows or []:
                    hits[as_str(r["id"])] = r
            except Exception:  # noqa: BLE001 - empty index etc.
                pass
        if self.embedder:
            vec = self.embedder(query)
            rows = self.q(f"""SELECT id, kind, text, turn.n AS turn, vector::distance::knn() AS dist
                              FROM memory WHERE embedding <|{k},40|> $v ORDER BY dist;""",
                          {"v": vec}, show=True)
            for r in rows or []:
                hits.setdefault(as_str(r["id"]), r)
        # always include what she knows the handler has (she would not forget that)
        for r in self.q("SELECT id, kind, text, turn.n AS turn FROM memory WHERE kind = 'learned';") or []:
            hits.setdefault(as_str(r["id"]), r)
        return list(hits.values())

    # ----------------------------------------------------------- the record
    def flags_since(self, turn_n: int) -> list[dict]:
        return self.q("""
            SELECT id, kind, certainty, overlap_min, reason,
                   in.quote AS quote, in.turn.n AS turn, in.place.name AS said_place,
                   in.start AS s, in.end AS e,
                   out AS against, out.code AS code, out.title AS title, out.quote AS other_quote,
                   out.turn.n AS other_turn
            FROM discrepancy WHERE in.turn.n >= $n ORDER BY turn, kind, overlap_min DESC;""", {"n": turn_n}, show=True) or []

    def all_flags(self) -> list[dict]:
        return self.flags_since(0)

    def claims(self, live_only: bool = False) -> list[dict]:
        where = "WHERE retracted = false" if live_only else ""
        return self.q(f"""SELECT id, quote, place.name AS place, place AS place_id, start, end, activity,
                                 retracted, confidence, turn.n AS turn,
                                 count(->discrepancy) AS hits
                          FROM claim {where} ORDER BY start;""") or []

    def supersessions(self) -> list[dict]:
        return self.q("""SELECT in.quote AS new, in.turn.n AS new_turn,
                                out.quote AS old, out.turn.n AS old_turn, at FROM supersedes ORDER BY at;""") or []

    def confidence(self) -> dict:
        return self.q("RETURN fn::confidence_level();") or {}

    def blame(self, turn_n: int) -> list[dict]:
        """Every claim from a turn, the words that made it, and every flag it raised."""
        return self.q("""
            SELECT id, quote, place.name AS place, start, end, confidence, retracted, said_at,
                   turn.n AS turn, turn.question AS question,
                   ->discrepancy.{kind, certainty, reason, overlap_min} AS flagged_by,
                   <-supersedes<-claim.{quote, turn: turn.n} AS replaced_by,
                   ->supersedes->claim.{quote, turn: turn.n} AS replaces
            FROM claim WHERE turn.n = $n ORDER BY start;""", {"n": turn_n}, show=True) or []

    def story_at(self, when: Any) -> list[dict]:
        """System time: the asset's live story exactly as the database held it at `when`."""
        rows = self.q("""SELECT id, quote, place.name AS place, start, end, turn.n AS turn
                         FROM claim WHERE retracted = false ORDER BY start VERSION <datetime> $t;""",
                      {"t": when}, show=True) or []
        # Claims are never deleted during a debrief, only when a new debrief resets a server database.
        # SurrealDB 3.3's VERSION table scan still returns rows deleted before $t, so keep this debrief's claims only.
        exists = {as_str(i) for i in self.q("SELECT VALUE id FROM claim;") or []}
        return [r for r in rows if as_str(r["id"]) in exists]

    def turn_time(self, n: int):
        r = self.q("SELECT VALUE at FROM $id;", {"id": rid("turn", n)})
        return r[0] if r else None

    def now(self):
        return self.q("RETURN time::now();")
