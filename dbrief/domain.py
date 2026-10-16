"""A domain: everything dataset-specific, read from domains/<name>.toml.

The engine never names a dataset. A domain says which CSV, which column is the record key,
what the entity is called, who is speaking, and which fields can be checked, each with a type.
The rule for each type lives in SurrealDB (ledger.surql); the domain only supplies parameters.

  datetime  near-miss within window_hours            hour      near-miss within window_hours (24h clock)
  number    near-miss within pct (or abs) of record   category  exact or alias match, else conflict
  place     exact or alias match, else conflict       text      exact match, else unverified
"""
from __future__ import annotations

import csv
import json
import os
import re
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAINS = ROOT / "domains"
STATE = ROOT / ".dbrief-state.json"
TYPES = ("datetime", "hour", "number", "category", "place", "text")
DEFAULT_RULES = {"datetime": {"window_hours": 1}, "hour": {"window_hours": 1}, "number": {"pct": 0.10}}


class Domain:
    def __init__(self, data: dict, path: Path | None = None):
        self.path = path
        self.name: str = data["name"]
        self.display: str = data.get("display", self.name)
        self.csv: str | None = data.get("csv")
        self.key: str = data["key"]
        self.entity: str = data.get("entity", "record")
        self.scope: str = data.get("scope", f"dbrief/{self.name}")
        self.speaker: str = data.get("speaker", "user")
        self.agent: str = data.get("agent", "agent")
        self.persona: str = data.get("persona", f"an agent that checks what people say against the {self.entity} records")
        self.demo_key: str | None = data.get("demo_key")
        self.opening: str | None = data.get("opening")
        rules = {t: dict(v) for t, v in DEFAULT_RULES.items()}
        for t, v in (data.get("rules") or {}).items():
            rules.setdefault(t, {}).update(v)
        self.fields: dict[str, dict] = {}
        for f, spec in (data.get("fields") or {}).items():
            t = spec.get("type", "text")
            if t not in TYPES:
                raise ValueError(f"{f}: unknown type {t!r} (one of {', '.join(TYPES)})")
            p = {**rules.get(t, {}), **{k: v for k, v in spec.items() if k in ("window_hours", "pct", "abs")}}
            self.fields[f] = {"type": t, "label": spec.get("label", f.replace("_", " ")),
                              "values": list(spec.get("values") or []), **p}
        self.aliases: dict[str, str] = {str(k).lower(): str(v) for k, v in (data.get("aliases") or {}).items()}
        self.demo_record: dict | None = data.get("demo_record")
        self.offline: list[dict] = list(data.get("offline") or [])
        self.autopilot: dict = dict(data.get("autopilot") or {})

    @classmethod
    def load(cls, name: str | None = None) -> "Domain":
        name = name or os.environ.get("DBRIEF_DOMAIN")
        if not name:  # no choice made: the only domain there is
            found = sorted(DOMAINS.glob("*.toml"))
            if len(found) != 1:
                raise SystemExit(f"pick a domain with --domain or DBRIEF_DOMAIN: {', '.join(p.stem for p in found) or 'none yet'}")
            name = found[0].stem
        path = Path(name) if name.endswith(".toml") else DOMAINS / f"{name}.toml"
        if not path.exists():
            have = ", ".join(sorted(p.stem for p in DOMAINS.glob("*.toml"))) or "none"
            raise SystemExit(f"no domain {name!r} ({path}); available: {have}. Create one with: dbrief load <csv> --key <column>")
        return cls(tomllib.loads(path.read_text()), path)

    # ---------------------------------------------------------------- naming
    def ref(self, key: str) -> str:
        return f"{self.entity}:{key}"

    def label(self, field: str) -> str:
        return self.fields.get(field, {}).get("label", field.split(":", 1)[-1].replace("_", " "))

    def conversation_scope(self, key: str, session: str | None = None) -> str:
        return f"{self.scope}/{self.speaker}/{key}" + (f"/{session}" if session else "")

    # ------------------------------------------------------------------ data
    def csv_path(self) -> Path | None:
        for p in (os.environ.get("DBRIEF_DATA"), self.csv and ROOT / self.csv, self.csv):
            if p and Path(p).exists():
                return Path(p)
        return None

    def record_fields(self, row: dict) -> dict[str, str]:
        """The checkable fields of one row. Unknown values ('', '?') are left out."""
        return {k: str(row[k]).strip() for k in self.fields if str(row.get(k, "")).strip() not in ("", "?")}

    def rows(self, n: int = 300) -> list[dict]:
        """The demo record first, then the dataset in file order (deterministic)."""
        path = self.csv_path()
        if not path:
            return [self.demo_record] if self.demo_record else []
        rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
        first = [r for r in rows if r.get(self.key) == self.demo_key]
        rest = [r for r in rows if r.get(self.key) != self.demo_key]
        return (first + rest)[:n]

    def row(self, key: str) -> dict | None:
        if self.demo_record and self.demo_record.get(self.key) == key:
            return self.demo_record
        return next((r for r in self.rows(10**9) if r.get(self.key) == key), None)

    # ----------------------------------------------------------------- state
    def loaded(self) -> dict:
        try:
            return json.loads(STATE.read_text()).get(self.name, {})
        except (OSError, ValueError):
            return {}

    def mark_loaded(self, rows: int, backend: str) -> None:
        try:
            state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            state = {}
        state[self.name] = {"rows": rows, "backend": backend, "at": datetime.now().isoformat(timespec="seconds")}
        STATE.write_text(json.dumps(state, indent=2))


# ================================================================ profiling ==
PLACE_WORDS = ("city", "state", "location", "address", "region", "country", "zip", "postcode", "street", "town")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def guess_type(name: str, values: list[str]) -> dict:
    vals = [v.strip() for v in values if v.strip() not in ("", "?")]
    distinct = sorted(set(vals))
    low = name.lower()
    if not vals:
        return {"type": "text"}
    if all(_NUM.match(v) for v in vals):
        if "hour" in low and all(0 <= float(v) <= 23 for v in vals):
            return {"type": "hour"}
        return {"type": "number"}
    if all(_DATE.match(v) for v in vals):
        return {"type": "datetime"}
    if any(w in low for w in PLACE_WORDS):
        return {"type": "place"}
    if len(distinct) <= 20 or len(distinct) <= 0.05 * len(vals):
        return {"type": "category", "values": distinct[:20]}
    return {"type": "text"}


def profile(csv_path: Path, key: str, sample: int = 2000) -> dict[str, dict]:
    reader = csv.DictReader(open(csv_path, newline="", encoding="utf-8-sig"))
    if key not in (reader.fieldnames or []):
        raise SystemExit(f"--key {key!r} is not a column of {csv_path.name}: {', '.join(reader.fieldnames or [])}")
    rows = [r for _, r in zip(range(sample), reader)]
    out = {}
    for col in reader.fieldnames or []:
        if col == key or not col.strip() or col.startswith("_"):
            continue
        out[col] = {**guess_type(col, [r.get(col) or "" for r in rows]), "label": col.replace("_", " ").replace("-", " ")}
    return out


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def write_draft(name: str, csv_path: Path, key: str, fields: dict[str, dict], demo_key: str | None) -> Path:
    """A starting domain file. Never overwrites: edit it by hand afterwards."""
    DOMAINS.mkdir(exist_ok=True)
    path = DOMAINS / f"{name}.toml"
    if path.exists():
        return path
    try:
        rel = os.path.relpath(csv_path.resolve(), ROOT)
    except ValueError:
        rel = str(csv_path.resolve())
    lines = [f"# DBrief domain {name!r}: a draft written by `dbrief load`. Check the types and labels, then edit freely.",
             f"name = {_toml(name)}", f"display = {_toml(name.replace('_', ' ').title())}", f"csv = {_toml(rel)}",
             f"key = {_toml(key)}", f"entity = {_toml(re.sub(r'[^a-z0-9]+', '_', key.lower()).removesuffix('_id').removesuffix('_number') or 'record')}",
             f"scope = {_toml('dbrief/' + name)}", 'speaker = "user"', 'agent = "agent"',
             f"persona = {_toml('an agent that checks what people tell it against the ' + name.replace('_', ' ') + ' records')}"]
    if demo_key:
        lines.append(f"demo_key = {_toml(demo_key)}")
    lines += ["", "[rules]", "datetime = { window_hours = 1 }", "hour = { window_hours = 1 }", "number = { pct = 0.10 }",
              "", "[fields]"]
    for f, spec in fields.items():
        body = ", ".join(f"{k} = {_toml(v)}" for k, v in spec.items())
        lines.append(f"{_toml(f) if not re.match(r'^[A-Za-z0-9_-]+$', f) else f} = {{ {body} }}")
    path.write_text("\n".join(lines) + "\n")
    return path
