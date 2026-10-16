"""Terminal rendering. A claims desk, not a chat window."""
from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .fields import FIELDS
from .ledger import hhmm

console = Console(highlight=False)

STATUS = {
    "consistent": ("✓", "green3"),
    "near-miss":  ("~", "yellow"),
    "conflict":   ("✗", "red1"),
    "unverified": ("?", "grey62"),
}


def clip(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def label(field: str) -> str:
    return FIELDS.get(field, {}).get("label", field.split(":", 1)[-1].replace("_", " "))


def status_text(status: str | None) -> Text:
    mark, style = STATUS.get(status or "", ("·", "grey50"))
    return Text(f"{mark} {status or '—'}", style=style)


# --------------------------------------------------------------- headers ----
def banner(mode: str, backend: str, note: str | None, policy: str) -> None:
    title = Text()
    title.append("DBrief", style="bold white")
    title.append("   the claimant talks · the database checks", style="italic grey62")
    console.print(Panel(title, box=box.HEAVY, border_style="grey35", padding=(0, 2)))
    meta = Text()
    meta.append(f"CLAIM  policy {policy}", style="bold")
    meta.append(f"   ·   memory: {backend}", style="bold cyan")
    meta.append(f"   ·   {mode}", style="grey62")
    console.print(meta)
    if note:
        console.print(Text(f"  ⚠ {note}", style="yellow"))
    memory = ("SurrealDB Agent Memory (authoritative + experiential, recall with traces)"
              if backend.startswith("SurrealDB Agent Memory") else
              f"{backend} (offline fallback: same recall / inspect / remember interface, local traces)")
    console.print(Text(f"  Memory: {memory}. Verification: deterministic rules in SurrealDB.", style="grey50"))
    console.print()


def review_bar(rv: dict) -> Text:
    t = Text("  CLAIM REVIEW ")
    t.append(f" ✓ {rv.get('consistent', 0)} consistent ", style="bold green3")
    t.append(f" ~ {rv.get('near_miss', 0)} near-miss ", style="bold yellow")
    t.append(f" ✗ {rv.get('conflict', 0)} conflict ", style="bold red1")
    t.append(f" ? {rv.get('unverified', 0)} unverified ", style="bold grey62")
    t.append(f"   corrected {rv.get('corrected', 0)} · flags {rv.get('flags', 0)}", style="grey50")
    return t


# ----------------------------------------------------------------- turns ----
def render_said(n: int, said: str, who: str = "CLAIMANT") -> None:
    console.print(Rule(Text(f" TURN {n:02d} ", style="bold black on grey70"), style="grey35", align="left"))
    t = Text(f"  {who}  ", style="bold cyan")
    t.append(said, style="cyan")
    console.print(t)


def memory_line(res: dict) -> Text:
    """What memory did for this answer: the receipt in one line."""
    recalls = res.get("recalls") or []
    t = Text("  memory · ", style="bold grey62")
    if not recalls:
        t.append(f"{res.get('backend')} · no recall: this answer cites nothing", style="grey50")
        return t
    hits = sum(len(r["hits"]) for r in recalls)
    t.append(f"{res['backend']} recall · {hits} hit{'s' if hits != 1 else ''}", style="grey62")
    cited = res.get("cited") or []
    if cited:
        flagged = next((c for c in res.get("claims", []) if c.get("record_value") is not None), None)
        detail = f" ({flagged['field']} = {flagged['record_value']})" if flagged else ""
        t.append(f" · cited {', '.join(cited)}{detail}", style="grey62")
    uncited = (res.get("trace") or {}).get("uncited") or []
    if uncited:
        t.append(f" · UNCITED {', '.join(uncited)}", style="bold red1")
    tids = [r["trace_id"] for r in recalls if r.get("trace_id")]
    if tids:
        t.append(f" · trace {tids[0]}" + (f" +{len(tids) - 1}" if len(tids) > 1 else ""), style="grey62")
    t.append(f" · {res.get('memory_ms', 0):.0f} ms", style="grey62")
    return t


def db_status(d: dict) -> Text:
    """The ledger at work this turn: what fn::report wrote and how long it took."""
    n = d["claims"]
    t = Text("  SurrealDB", style="bold grey62")
    if not n:
        t.append(" · no claims written", style="grey50")
        return t
    t.append(f" · {n} claim{'s' if n != 1 else ''} written"
             f" · check_claim fired{f' {n}×' if n > 1 else ''}"
             f" · {d['edges']} edge{'s' if d['edges'] != 1 else ''}"
             f" · {d['ms']:.1f} ms", style="grey50")
    return t


def render_turn(res: dict) -> None:
    console.print(Panel(Text(res["answer"], style="white"), title="[bold green3]DBRIEF[/]", title_align="left",
                        border_style="green4", box=box.ROUNDED, padding=(0, 2)))
    console.print(memory_line(res))
    claims = res.get("claims") or []
    if claims:
        tab = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False)
        tab.add_column(width=9)
        tab.add_column(width=24)
        tab.add_column(width=18)
        tab.add_column(width=14)
        tab.add_column()
        for c in claims:
            if c.get("retracted"):
                continue
            rec = c.get("record_value")
            tab.add_row(Text("  filed", style="grey50"), Text(label(c["field"]), style="white"),
                        Text(clip(c["value"], 18), style="bold white"), status_text(c.get("status")),
                        Text(f"record: {rec}" if rec is not None else "record: —", style="grey62"))
        console.print(tab)
    else:
        console.print(Text("  filed     no factual claims in that", style="grey50"))
    for x in res.get("superseded") or []:
        body = Text.assemble(("CORRECTED  ", "bold blue"), (f"{label(x['field'])}: ", "grey62"),
                             (f"{x['old_value']}", "grey62 strike"), ("  →  ", "grey50"),
                             (f"{x['new_value']}", "bold white"), (f"  ({x['new_status']})", "green3"))
        body.append(f"\n           turn {x['old_turn']} claim retracted, not deleted: “{x['old']}” stays on the record",
                    style="grey50")
        console.print(Panel(body, border_style="blue", box=box.SQUARE, padding=(0, 1)))
    for f in res.get("flags") or []:
        console.print(flag_panel(f))
    console.print(db_status(res["db"]))
    console.print(review_bar(res["review"]))
    console.print()


def flag_panel(f: dict, idx: int | None = None) -> Panel:
    potential = f.get("certainty") == "potential"
    if f["kind"] == "record":
        head = "NEAR-MISS · CHECK WITH CLAIMANT" if f["status"] == "near-miss" else "CONFLICT WITH RECORD"
        against = f"{f.get('ref')} · trace {f.get('trace_id') or '—'}"
    else:
        head = "CLAIMANT CHANGED THEIR ACCOUNT"
        against = f"their turn {f.get('other_turn')}: “{clip(f.get('other_quote') or '', 50)}”"
    style = "yellow" if potential else "red1"
    body = Text()
    body.append(f"turn {f['turn']}  ", style="grey50")
    body.append(f"“{f['quote']}”", style="white")
    body.append(f"\n  {label(f['field'])}: said {f['claimed']}  ·  {'record' if f['kind'] == 'record' else 'earlier'}: "
                f"{f['expected']}", style=style)
    title = f"[bold {style}]{('#' + str(idx) + '  ') if idx is not None else ''}{head}[/]"
    return Panel(body, title=title, title_align="left", subtitle=f"[grey50]{against}[/]", subtitle_align="right",
                 border_style=style, box=box.ROUNDED if potential else box.HEAVY, padding=(0, 1))


# ----------------------------------------------------------- inspections ----
def render_flag_list(rows: list[dict]) -> None:
    if not rows:
        console.print(Text("  No flags on the record yet.", style="grey62"))
        return
    for i, f in enumerate(rows, 1):
        console.print(flag_panel(f, i))
    console.print(Text("  /why N  traces flag N back to the memory record", style="grey50"))


def render_board(ledger) -> None:
    """Field by field: what the claimant said (and corrected), what the record says, the verdict."""
    rows: dict[str, list[dict]] = {}
    for c in ledger.claims():
        rows.setdefault(c["field"], []).append(c)
    tab = Table(box=box.SIMPLE_HEAD, padding=(0, 1), title="[bold]CLAIM BOARD[/]  "
                "[grey50]the claimant's words vs the record recalled from memory[/]", title_justify="left")
    for col in ("field", "claimant said", "record (memory)", "verdict", "turn"):
        tab.add_column(col)
    for field, cs in rows.items():
        said = Text()
        for c in cs:
            if said:
                said.append("  →  ", style="grey50")
            said.append(c["value"], style="grey50 strike" if c["retracted"] else "bold white")
        live = next((c for c in reversed(cs) if not c["retracted"]), cs[-1])
        rec = live.get("record_value")
        tab.add_row(label(field), said, Text(str(rec) if rec is not None else "—", style="grey70"),
                    status_text(live.get("status")), ", ".join(str(c["turn"]) for c in cs))
    console.print(tab)
    console.print(review_bar(ledger.review()))


def render_trace(ledger, memory, n: int) -> None:
    """The receipt: the memory traces an answer used, what they retrieved, and the records it cited."""
    tr = ledger.trace_for(n)
    if not tr:
        console.print(Text(f"  No trace for turn {n}.", style="grey62"))
        return
    body = Text()
    body.append(f"turn {n} · memory: {tr['backend']} · {tr['memory_ms']:.0f} ms in memory calls\n", style="grey50")
    if not tr["trace_ids"]:
        body.append("no recall this turn: nothing could be cited\n", style="yellow")
    for tid in tr["trace_ids"]:
        try:
            t = memory.trace(tid)
        except Exception as e:  # noqa: BLE001
            body.append(f"\ntrace {tid}: could not fetch ({e})\n", style="yellow")
            continue
        body.append(f"\nTRACE {tid}", style="bold")
        body.append(f"   “{t.get('query')}” · tier {t.get('tier')} · {t.get('latency_ms') or 0:.0f} ms\n", style="grey62")
        for x in t["returned"][:8]:
            body.append(f"  {x.get('rank') or '-':>2}. {clip(x.get('ref') or x.get('id'), 46):46} ", style="cyan")
            body.append(f"{clip(x.get('label') or '', 44)}\n", style="white")
    body.append("\nCITED\n", style="bold")
    for ref in tr["cited"]:
        bad = ref in tr["uncited"]
        body.append(f"  {ref}", style="bold red1" if bad else "bold green3")
        body.append("   NOT retrieved by any recall this turn (uncited)\n" if bad else "   retrieved ✓\n",
                    style="red1" if bad else "grey50")
        if ":" in ref and not bad:
            rec = memory.inspect(ref)
            for k in [c["field"] for c in ledger.claims_for_turn(n)] or list(rec["fields"])[:6]:
                if k in rec["fields"]:
                    body.append(f"      {k} = {rec['fields'][k]}   ", style="white")
                    body.append(f"{rec['attr_ids'].get(k, '')}\n", style="grey42")
    if not tr["cited"]:
        body.append("  nothing cited\n", style="grey62")
    body.append("\nretrieved ⊇ cited is checked by the database (trace.uncited), not by a model.", style="grey50")
    console.print(Panel(body, title=f"[bold]RECEIPT · turn {n}[/]", title_align="left", border_style="cyan",
                        box=box.HEAVY, padding=(0, 1)))


def render_inspect(memory, ref: str) -> None:
    rec = memory.inspect(ref if ":" in ref else f"policy:{ref}")
    if not rec["found"]:
        console.print(Text(f"  {rec['ref']}: not in memory.", style="yellow"))
        return
    tab = Table(box=box.SIMPLE_HEAD, padding=(0, 1), title=f"[bold]{rec['ref']}[/]  [grey50]{memory.backend} · "
                f"{rec['ms']:.0f} ms[/]", title_justify="left")
    for col in ("field", "value", "memory id"):
        tab.add_column(col)
    for k in sorted(rec["fields"]):
        tab.add_row(k, Text(str(rec["fields"][k]), style="bold white"), Text(str(rec["attr_ids"].get(k, "")), style="grey42"))
    console.print(tab)


def render_why(ledger, memory, f: dict, idx: int) -> None:
    """Flag -> the words -> the trace that recalled the record -> the record value."""
    turn = ledger.q("SELECT n, question, answer, at FROM turn WHERE n = $n;", {"n": f["turn"]})[0]
    body = Text()
    body.append("WHAT THE CLAIMANT SAID\n", style="bold")
    body.append(f"  turn {turn['n']} · {hhmm(turn['at'])} real time\n", style="grey50")
    body.append(f"  “{turn['question']}”\n", style="cyan")
    body.append("\nWHAT WAS FILED\n", style="bold")
    body.append(f"  “{f['quote']}”  →  {f['field']} = {f['claimed']}\n", style="white")
    if f["kind"] == "record":
        body.append("\nTHE MEMORY TRACE IT WAS CHECKED AGAINST\n", style="bold")
        tid = f.get("trace_id")
        if tid:
            try:
                t = memory.trace(tid)
                body.append(f"  {tid} · “{t.get('query')}” · {len(t['returned'])} returned\n", style="cyan")
                for x in t["returned"]:
                    if f["field"] in str(x.get("ref")) or f["field"] in str(x.get("label")):
                        body.append(f"  → {x.get('ref')}  {x.get('label')}\n", style="bold cyan")
            except Exception as e:  # noqa: BLE001
                body.append(f"  {tid} (could not fetch: {e})\n", style="yellow")
        rec = ledger.record(f.get("ref") or "")
        body.append("\nTHE RECORD (as memory returned it)\n", style="bold")
        if rec:
            body.append(f"  {rec['ref']} · {f['field']} = {rec['fields'].get(f['field'])}"
                        f" · {rec['attr_ids'].get(f['field'], '')} · via {rec['source']}\n", style="white")
    else:
        body.append(f"\nWHAT THEY SAID BEFORE\n  turn {f['other_turn']}: “{f['other_quote']}” ({f['expected']})\n",
                    style="white")
    body.append(f"\nVERDICT  {f['status']} ({f['certainty']})", style="bold red1" if f["status"] == "conflict" else "bold yellow")
    body.append("   decided by the check_claim event inside SurrealDB, in the claim's write transaction. "
                "No model was asked.", style="grey50")
    console.print(Panel(body, title=f"[bold]#{idx} PROVENANCE[/]", title_align="left", border_style="red1",
                        box=box.HEAVY, padding=(0, 1)))


def render_blame(ledger, n: int) -> None:
    rows = ledger.blame(n)
    if not rows:
        console.print(Text(f"  Turn {n} filed no claims.", style="grey62"))
        return
    for r in rows:
        body = Text()
        body.append(f"“{r['question']}”\n", style="cyan")
        body.append(f"“{r['quote']}”  →  {r['field']} = {r['value']}  ", style="white")
        body.append_text(status_text(r["status"]))
        body.append(f"   record {r['record_value']} · extraction {r['confidence']:.0%} · trace {r['trace_id']}\n",
                    style="grey62")
        for x in r.get("flagged_by") or []:
            body.append(f"  ✗ [{x['kind']}/{x['status']}/{x['certainty']}] {x['reason']}\n", style="red1")
        for x in r.get("replaced_by") or []:
            body.append(f"  ⤷ retracted at turn {x['turn']}: “{x['quote']}” ({x['value']})\n", style="blue")
        for x in r.get("replaces") or []:
            body.append(f"  ⤶ replaced turn {x['turn']}: “{x['quote']}” ({x['value']})\n", style="blue")
        console.print(Panel(body, title=f"[bold]BLAME · turn {n} · {label(r['field'])}[/]", title_align="left",
                            border_style="grey50", box=box.ROUNDED, padding=(0, 1)))


def render_rewind(ledger, n: int) -> None:
    """System time: the claimant's live account as the ledger held it at the end of turn n."""
    nxt = ledger.turn_time(n + 1)
    when = nxt if nxt is not None else ledger.now()
    try:
        then = ledger.story_at(when)
    except Exception as e:  # noqa: BLE001
        console.print(Text(f"  Time travel needs a versioned store (surrealkv+versioned://). {e}", style="yellow"))
        return
    now = ledger.claims(live_only=True)
    tab = Table(box=box.SIMPLE_HEAD, padding=(0, 1), title=f"[bold]REWIND[/]  [grey50]SELECT … VERSION "
                f"<end of turn {n}>  vs  now[/]", title_justify="left")
    tab.add_column(f"their account after turn {n}", ratio=1)
    tab.add_column("their account now", ratio=1)
    fmt = lambda c: Text.assemble((f"{label(c['field'])} = {c['value']}  ", "white"), status_text(c["status"]))
    for i in range(max(len(then), len(now))):
        tab.add_row(fmt(then[i]) if i < len(then) else Text(""), fmt(now[i]) if i < len(now) else Text(""))
    console.print(tab)


def render_memory(memory, policy: str) -> None:
    from .memory import claimant_scope
    r = memory.recall(f"what the claimant on policy {policy} said", scopes=[claimant_scope(policy)], k=10)
    tab = Table(box=box.SIMPLE_HEAD, padding=(0, 1), title=f"[bold]EXPERIENTIAL MEMORY[/]  [grey50]scope "
                f"{claimant_scope(policy)} · trace {r.get('trace_id')} · {r['ms']:.0f} ms[/]", title_justify="left")
    tab.add_column("id")
    tab.add_column("what memory returned")
    for h in r["hits"]:
        tab.add_row(Text(clip(h["id"], 34), style="grey50"), h["text"])
    console.print(tab)


def render_sql(ledger) -> None:
    from rich.syntax import Syntax
    from .ledger import SCHEMA
    start = SCHEMA.index("-- The verdict for one claimed value")
    end = SCHEMA.index("-- =============================================================== REPORT")
    console.print(Panel(Syntax(SCHEMA[start:end].strip(), "sql", theme="ansi_dark", word_wrap=True),
                        title="[bold]the checks · ledger.surql[/]", title_align="left", border_style="grey50"))
    if ledger.queries:
        console.print(Text("last query run:", style="grey50"))
        console.print(Syntax(ledger.queries[-1], "sql", theme="ansi_dark", word_wrap=True))


HELP = """[bold]Talk as the claimant, in plain words.[/] Commands:
  [cyan]/board[/]             field by field: what you said vs the record, and the verdict
  [cyan]/flags[/]             every near-miss and conflict on record
  [cyan]/why N[/]             flag N -> your words -> the memory trace -> the record
  [cyan]/trace T[/]           the receipt for turn T: what memory retrieved, what was cited
  [cyan]/inspect REF[/]       read a record from memory, e.g. /inspect policy:521585
  [cyan]/blame T[/]           every claim from turn T and what it collided with
  [cyan]/rewind T[/]          time travel: the account as the ledger held it after turn T
  [cyan]/memory[/]            experiential memory: what memory holds about this conversation
  [cyan]/sql[/]               the SurrealQL rules that decide each verdict
  [cyan]/review[/]            the claim-review summary
  [cyan]/quit[/]"""


def render_summary(rv: dict) -> None:
    console.print(Panel(review_bar(rv), title="[bold]CLAIM REVIEW[/]", title_align="left",
                        border_style="cyan", box=box.DOUBLE, padding=(0, 1)))
    console.print(Text("  The claimant talks. The database checks. Every verdict traces back to a memory record.",
                       style="bold"))
