"""The agent's memory. Two backends, one interface:

  recall(query, scopes, k) -> hits + trace_id      what memory returned, and the receipt
  inspect(ref)             -> a record's fields     e.g. inspect("<entity>:<key>")
  remember(text, scopes)   -> store experiential memory (what the speaker said)
  trace(trace_id)          -> what a recall retrieved, from the memory's own trace log

AgentMemory talks to hosted SurrealDB Agent Memory over REST. LocalMemory keeps the same
shape in an embedded SurrealDB, for offline runs and as the automatic fallback.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    """Environment first, then the git-ignored .env file. Values are never printed."""
    if os.environ.get(name):
        return os.environ[name]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def entity_ref(entity_type: str, name: str) -> str:
    return f"{entity_type}:{name}"


def _split(ref: str) -> tuple[str, str]:
    etype, _, name = ref.partition(":")
    if not name:
        raise ValueError(f"expected <entity>:<key>, got {ref!r}")
    return etype, name


# ============================================================ AGENT MEMORY ====
class MemoryError_(RuntimeError):
    pass


class AgentMemory:
    backend = "SurrealDB Agent Memory"

    def __init__(self, url: str, context_id: str, key: str, timeout: float = 60):
        self.base = f"{url.rstrip('/')}/api/v1/{context_id}"
        self._key = key
        self.timeout = timeout
        self._scopes: set[str] = set()

    @classmethod
    def from_env(cls) -> "AgentMemory | None":
        url = env("SPECTRON_URL", "https://srv1.spectron.aws-usw2.surreal.cloud")
        ctx = env("SPECTRON_CONTEXT_ID", "06gkauiei1u412cclcdhr470co")
        key = env("SPECTRON_API_KEY")
        return cls(url, ctx, key) if url and ctx and key else None

    def _call(self, method: str, path: str, body: Any = None) -> tuple[Any, float]:
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300].replace(self._key, "<redacted>")
            raise MemoryError_(f"{method} {path}: HTTP {e.code} {msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MemoryError_(f"{method} {path}: {e}") from None
        ms = (time.perf_counter() - t0) * 1000
        return (json.loads(raw) if raw else {}), ms

    def ensure_scope(self, path: str) -> None:
        if path in self._scopes:
            return
        try:
            self._call("POST", "/scopes", {"path": path})
        except MemoryError_ as e:
            if "HTTP 409" not in str(e):  # already exists
                raise
        self._scopes.add(path)

    # --- the interface
    def recall(self, query: str, scopes: list[str], k: int = 8) -> dict:
        data, ms = self._call("POST", "/query", {"query": query, "k": k, "lens": scopes})
        hits = []
        for h in data.get("hits", []):
            res = h.get("resource") or {}
            ent = entity_ref(res["entityType"], res["name"]) if res.get("entityType") and res.get("name") else None
            hits.append({"id": h.get("id"), "text": h.get("text", ""), "kind": h.get("source"),
                         "entity": ent, "key": res.get("key"), "score": h.get("score")})
        tr = data.get("trace") or {}
        return {"backend": self.backend, "hits": hits, "trace_id": tr.get("traceId"),
                "tier": tr.get("resolutionTier") or data.get("tier"), "ms": ms}

    def inspect(self, ref: str) -> dict:
        etype, name = _split(ref)
        try:
            data, ms = self._call("GET", f"/entities/{etype}/{name}")
        except MemoryError_ as e:
            if "HTTP 404" in str(e):
                return {"ref": entity_ref(etype, name), "found": False, "fields": {}, "attr_ids": {}, "ms": 0.0}
            raise
        fields, ids = {}, {}
        for a in data.get("attributes", []):
            if a.get("supersededBy"):
                continue
            fields[a["key"]] = a.get("value")
            ids[a["key"]] = f"attribute:{a.get('id')}" if a.get("id") and ":" not in str(a.get("id")) else a.get("id")
        return {"ref": entity_ref(etype, name), "found": bool(data.get("entity")), "fields": fields,
                "attr_ids": ids, "ms": ms}

    def remember(self, text: str, scopes: list[str]) -> dict:
        for s in scopes:
            self.ensure_scope(s)
        data, ms = self._call("POST", "/facts", {"text": text, "infer": "none", "scopes": scopes})
        return {"ms": ms, "turn_id": data.get("turnId")}

    def trace(self, trace_id: str) -> dict:
        data, ms = self._call("GET", f"/traces/{trace_id}")
        retrieved, returned = [], []
        for r in data.get("retrievals", []):
            retrieved += r.get("retrievedIds", [])
            returned += [{"id": x.get("id"), "ref": x.get("ref"), "label": x.get("label"),
                          "score": x.get("score"), "rank": x.get("rank")} for x in r.get("returned", [])]
        return {"trace_id": data.get("id", trace_id), "query": data.get("queryText"),
                "tier": data.get("resolutionTier"), "reason": data.get("tierReason"),
                "latency_ms": data.get("latencyMs"), "retrieved": retrieved, "returned": returned, "ms": ms}

    # --- authoritative load
    def load(self, domain, rows: list[dict], batch: int = 200, log=print) -> int:
        self.ensure_scope(domain.scope)
        n = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            triples = [{"entity": {"type": domain.entity, "name": str(r[domain.key])}, "key": k, "value": v}
                       for r in chunk for k, v in domain.record_fields(r).items()]
            _, ms = self._call("POST", "/facts", {
                "text": f"{domain.display}: {domain.entity} {chunk[0][domain.key]}..{chunk[-1][domain.key]}",
                "infer": "triples", "triples": triples, "scopes": [domain.scope]})
            n += len(chunk)
            log(f"  batch {i // batch + 1}: {len(chunk)} {domain.entity} records, {len(triples)} triples, {ms:.0f} ms")
        return n


# ============================================================ LOCAL MEMORY ====
LOCAL_SCHEMA = """
DEFINE ANALYZER OVERWRITE mem_text TOKENIZERS blank, class, punct FILTERS lowercase, ascii, snowball(english);
DEFINE TABLE OVERWRITE mem_attr SCHEMALESS;
DEFINE TABLE OVERWRITE mem_note SCHEMALESS;
DEFINE TABLE OVERWRITE mem_trace SCHEMALESS;
"""


class LocalMemory:
    """Same interface, embedded SurrealDB. Persists in .dbrief-memory so --resume works offline too."""
    backend = "local SurrealDB memory"

    def __init__(self, domain, url: str | None = None):
        from surrealdb import Surreal
        self.domain = domain
        self.url = url or env("DBRIEF_MEMORY_URL", "surrealkv://.dbrief-memory")
        self.db = Surreal(self.url)
        self.db.use("dbrief", "memory")
        self.db.query(LOCAL_SCHEMA)
        for name, table, field in (("mem_attr_ft", "mem_attr", "text"), ("mem_note_ft", "mem_note", "text")):
            try:
                self.db.query(f"DEFINE INDEX OVERWRITE {name} ON {table} FIELDS {field} FULLTEXT ANALYZER mem_text BM25;")
            except Exception:  # noqa: BLE001 - SurrealDB 2.x syntax
                self.db.query(f"DEFINE INDEX OVERWRITE {name} ON {table} FIELDS {field} SEARCH ANALYZER mem_text BM25;")
        if not self.db.query("SELECT VALUE id FROM mem_attr WHERE scope = $s LIMIT 1;", {"s": domain.scope + "/"}):
            self.load(domain, domain.rows(), log=lambda *_: None)

    @staticmethod
    def _terms(query: str) -> list[str]:
        stop = set("a an the and or of to in on at for is was were what which who my me i it this that "
                   "record about with did do does".split())
        return [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in stop][:8]

    def recall(self, query: str, scopes: list[str], k: int = 8) -> dict:
        t0 = time.perf_counter()
        terms = self._terms(query) or [query.lower()]
        where = " OR ".join(f"text @{i + 1}@ $t{i}" for i in range(len(terms)))
        params = {f"t{i}": t for i, t in enumerate(terms)}
        rows: list[dict] = []
        for table in ("mem_attr", "mem_note"):
            score = " + ".join(f"search::score({i + 1})" for i in range(len(terms)))
            try:
                rows += self.db.query(f"""SELECT *, ({score}) AS score, '{table}' AS tbl FROM {table}
                                          WHERE ({where}) ORDER BY score DESC LIMIT {k * 4};""", params) or []
            except Exception:  # noqa: BLE001 - empty index
                pass
        rows = [r for r in rows if any(str(r.get("scope", "")).startswith(s) for s in scopes)]
        rows.sort(key=lambda r: -(r.get("score") or 0))
        hits = []
        for r in rows[:k]:
            rid = f"{'attribute' if r['tbl'] == 'mem_attr' else 'note'}:{r['id'].id}"
            hits.append({"id": rid, "text": r["text"], "kind": "attribute" if r["tbl"] == "mem_attr" else "note",
                         "entity": entity_ref(r["etype"], r["name"]) if r.get("name") else None,
                         "key": r.get("key"), "score": round(float(r.get("score") or 0), 3)})
        ms = (time.perf_counter() - t0) * 1000
        tid = "local-" + uuid.uuid4().hex[:12]
        self.db.query("CREATE mem_trace CONTENT $c;", {"c": {
            "trace_id": tid, "query": query, "scopes": scopes, "latency_ms": ms,
            "returned": [{"id": h["id"], "ref": (h["entity"] + "/" + h["key"]) if h.get("key") else (h["entity"] or h["id"]),
                          "label": f"{h['key']} = {h['text'].rsplit(' is ', 1)[-1]}" if h.get("key") else h["text"][:80],
                          "score": h["score"], "rank": i + 1} for i, h in enumerate(hits)]}})
        return {"backend": self.backend, "hits": hits, "trace_id": tid, "tier": "local-bm25", "ms": ms}

    def inspect(self, ref: str) -> dict:
        t0 = time.perf_counter()
        etype, name = _split(ref)
        rows = self.db.query("SELECT id, key, value FROM mem_attr WHERE etype = $t AND name = $n;",
                             {"t": etype, "n": name}) or []
        return {"ref": entity_ref(etype, name), "found": bool(rows),
                "fields": {r["key"]: r["value"] for r in rows},
                "attr_ids": {r["key"]: f"attribute:{r['id'].id}" for r in rows},
                "ms": (time.perf_counter() - t0) * 1000}

    def remember(self, text: str, scopes: list[str]) -> dict:
        t0 = time.perf_counter()
        for s in scopes:
            self.db.query("CREATE mem_note CONTENT $c;", {"c": {"text": text, "scope": s}})
        return {"ms": (time.perf_counter() - t0) * 1000, "turn_id": None}

    def trace(self, trace_id: str) -> dict:
        t0 = time.perf_counter()
        rows = self.db.query("SELECT * FROM mem_trace WHERE trace_id = $t;", {"t": trace_id}) or []
        if not rows:
            raise MemoryError_(f"no local trace {trace_id}")
        t = rows[0]
        return {"trace_id": trace_id, "query": t["query"], "tier": "local-bm25", "reason": "local fallback",
                "latency_ms": t["latency_ms"], "retrieved": [x["id"] for x in t["returned"]],
                "returned": t["returned"], "ms": (time.perf_counter() - t0) * 1000}

    def load(self, domain, rows: list[dict], batch: int = 200, log=print) -> int:
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            docs = [{"etype": domain.entity, "name": str(r[domain.key]), "key": k, "value": v, "scope": domain.scope + "/",
                     "text": f"{r[domain.key]}'s {domain.label(k)} is {v}"}
                    for r in chunk for k, v in domain.record_fields(r).items()]
            self.db.query("INSERT INTO mem_attr $docs;", {"docs": docs})
            log(f"  local batch {i // batch + 1}: {len(chunk)} {domain.entity} records, {len(docs)} attributes")
        return len(rows)


# ============================================================== selection ====
class FallbackMemory:
    """Hosted Agent Memory, falling back to local memory for good if a call fails."""

    def __init__(self, primary: AgentMemory, local_factory):
        self.primary = primary
        self._local_factory = local_factory
        self.local = None
        self.fallback_reason: str | None = None

    @property
    def backend(self) -> str:
        return self.local.backend if self.local else self.primary.backend

    def _do(self, name: str, *a, **kw):
        if self.local is None:
            try:
                return getattr(self.primary, name)(*a, **kw)
            except MemoryError_ as e:
                self.fallback_reason = str(e)
                self.local = self._local_factory()
        return getattr(self.local, name)(*a, **kw)

    def recall(self, *a, **kw):
        return self._do("recall", *a, **kw)

    def inspect(self, *a, **kw):
        return self._do("inspect", *a, **kw)

    def remember(self, *a, **kw):
        return self._do("remember", *a, **kw)

    def trace(self, trace_id: str):
        if trace_id and trace_id.startswith("local-"):
            return (self.local or self._local_factory()).trace(trace_id)
        return self._do("trace", trace_id)


def open_memory(domain, offline: bool = False) -> tuple[Any, str | None]:
    """Returns (memory, note). note explains a fallback, for the banner."""
    if offline:
        return LocalMemory(domain), "offline: local SurrealDB memory"
    hosted = AgentMemory.from_env()
    if not hosted:
        return LocalMemory(domain), "SPECTRON_* not set: local SurrealDB memory"
    return FallbackMemory(hosted, lambda: LocalMemory(domain)), None
