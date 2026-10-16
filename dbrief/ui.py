"""Terminal rendering. A handler's workstation, not a chat window."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .db import DossierDB, hhmm, rid

console = Console(highlight=False)

PLACE_STYLE = {
    "Lumière": "gold1",
    "Safe house": "red1",
    "The Blue Note": "deep_sky_blue1",
    "In transit": "grey70",
    "Home": "green3",
}
SLOT_MIN = 10
DAY_START = "20:00"
DAY_END = "23:30"


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def pstyle(place: str | None) -> str:
    return PLACE_STYLE.get(place or "", "white")


def _dt(v) -> datetime:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


# --------------------------------------------------------------- headers ----
def banner(db: DossierDB, mode: str) -> None:
    dossier = db.dossier
    s = dossier["asset"]
    title = Text()
    title.append("D B R I E F", style="bold white")
    title.append("   a contradiction engine for agent memory, shown as a debrief", style="italic grey62")
    console.print(Panel(title, box=box.HEAVY, border_style="grey35", padding=(0, 2)))
    meta = Text()
    meta.append(f"DOSSIER  {dossier['title'].upper()}", style="bold")
    meta.append(f"   ·   ASSET  {s['name']} · {s['codename']}", style="bold red1")
    meta.append(f"   ·   {mode}", style="grey62")
    console.print(meta)
    console.print(Text(dossier["intro"], style="grey82"), width=min(console.width, 110))
    console.print()


def confidence_bar(st: dict, threshold: int, final: bool = False) -> Text:
    score = int(st.get("score", 0))
    filled = min(score, threshold)
    t = Text("  CONFIDENCE ")
    t.append("■" * filled, style="bold red1" if score >= threshold else "bold yellow")
    t.append("□" * max(threshold - filled, 0), style="grey35")
    t.append(f"  {score}/{threshold}", style="bold")
    t.append(f"   intel used {st.get('intel_hit', 0)} · inconsistencies {st.get('self_discrepancies', 0)}"
             f" · story changes {st.get('story_changes', 0)}", style="grey62")
    leads = st.get("leads") or []
    if leads:
        t.append(f"   leads: {', '.join(sorted(leads))}", style="bold magenta")
    if score >= threshold and not final:
        t.append("   ▶ /burn", style="bold red1 blink")
    return t


# ----------------------------------------------------------------- turns ----
def render_question(n: int, q: str, who: str = "HANDLER", target: str | None = None) -> None:
    console.print(Rule(Text(f" TURN {n:02d} ", style="bold black on grey70"), style="grey35", align="left"))
    if target:
        console.print(Text(f"  ⌖ {target}", style="italic grey62"))
    t = Text(f"  {who}  ", style="bold cyan")
    t.append(q, style="cyan")
    console.print(t)


def render_turn(res: dict, threshold: int, verbose_memory: bool = True) -> None:
    recalled = res.get("recalled") or []
    if verbose_memory:
        if res.get("memory", True):
            line = Text(f"  ↺ memory  ", style="grey50")
            line.append(f"{len(recalled)} recalled" if recalled else "nothing recalled", style="grey62")
            for lk in res.get("lookups", []):
                line.append(f" · looked up “{lk}”", style="grey62")
            console.print(line)
            for r in recalled[:4]:
                console.print(Text(f"      {r['kind']:>8} │ {clip(r['text'], 110)}", style="grey42"))
        else:
            console.print(Text("  ↺ memory  OFF (ablation): she sees only the question", style="grey50"))

    console.print(Panel(Text(f"“{res['answer']}”", style="italic white"),
                        title=f"[bold red1]{res['asset']}[/]", title_align="left",
                        border_style="red3", box=box.ROUNDED, padding=(0, 2)))

    claims = res.get("filed") or []
    if claims:
        tab = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False)
        tab.add_column(width=10)
        tab.add_column(width=18)
        tab.add_column(width=12)
        tab.add_column()
        for c in claims:
            conf = c.get("confidence", 1.0)
            tab.add_row(Text("  filed", style="grey50"),
                        Text(c["place_name"], style=pstyle(c["place_name"])),
                        Text(f"{c['start']}–{c['end']}", style="white"),
                        Text(f"“{clip(c['quote'], 70)}”" + (f"  (extraction {conf:.0%})" if conf < 0.7 else ""),
                             style="grey62"))
        console.print(tab)
    else:
        console.print(Text("  filed     no claims about her whereabouts on the night", style="grey50"))

    sup = res.get("superseded") or []
    if sup:
        body = Text.assemble(("STORY CHANGED  ", "bold blue"), ("now: ", "grey50"), (f"“{sup[0]['new']}”", "white"))
        for x in sup:
            body.append(f"\n               retracted turn {x['old_turn']}: ", style="grey50")
            body.append(f"“{x['old']}”", style="grey62 strike")
        body.append("\n               retracted, not deleted: the old words stay on the record", style="grey50")
        console.print(Panel(body, border_style="blue", box=box.SQUARE, padding=(0, 1)))

    new = res.get("flags") or []
    if not new and claims:
        console.print(Text(f"  ✓ consistent with the record  "
                           f"(checked against {res.get('n_intel', 0)} intel items and "
                           f"{res.get('n_prior', 0)} earlier claims)", style="bold green3"))
    leads = [c for c in new if c["kind"] == "intel" and not c.get("revealed", True)]
    for c in new:
        if c not in leads:
            console.print(flag_panel(c))
    if leads:
        line = Text("  ◆ lead   ", style="bold magenta")
        line.append("the database flags this answer against intel you haven't shown her: ", style="magenta")
        line.append(", ".join(f"{c['code']} {c['title']}" for c in leads), style="bold magenta")
        console.print(line)
    if res.get("db"):
        console.print(db_status(res["db"]))
    console.print(confidence_bar(res["confidence"], threshold))
    console.print()


def db_status(d: dict) -> Text:
    """The database at work this turn: what fn::report wrote and how long it took."""
    n = d["claims"]
    t = Text("  SurrealDB", style="bold grey62")
    if not n:
        t.append(" · no claims written", style="grey50")
        return t
    t.append(f" · {n} claim{'s' if n != 1 else ''} written"
             f" · raise_flags fired{f' {n}×' if n > 1 else ''}"
             f" · {d['edges']} edge{'s' if d['edges'] != 1 else ''}"
             f" · {d['ms']:.1f} ms", style="grey50")
    return t


def flag_panel(c: dict, idx: int | None = None) -> Panel:
    potential = c.get("certainty") == "potential"
    if c["kind"] == "intel":
        revealed = c.get("revealed", True)
        head = ("POTENTIAL FLAG · INTEL" if potential else "FLAG · CONTRADICTS INTEL")
        style = "yellow" if potential else ("red1" if revealed else "magenta")
        against = f"{c.get('code')} · {c.get('title')}"
        if not revealed:
            head += "  ·  not yet shown to her (lead)"
    else:
        head = "POTENTIAL FLAG · HER OWN DEBRIEF" if potential else "FLAG · CONTRADICTS HER OWN DEBRIEF"
        style = "yellow" if potential else "orange1"
        against = f"her turn {c.get('other_turn')}: “{(c.get('other_quote') or '')[:60]}”"
    body = Text()
    body.append(f"turn {c['turn']}  ", style="grey50")
    body.append(f"“{c['quote']}”", style="white")
    body.append(f"\n  ✗ {c['reason']}", style=style)
    body.append(f"   (overlap {c['overlap_min']} min)", style="grey50")
    title = f"[bold {style}]{('#' + str(idx) + '  ') if idx is not None else ''}{head}[/]"
    return Panel(body, title=title, title_align="left", subtitle=f"[grey50]{against}[/]",
                 subtitle_align="right", border_style=style, box=box.HEAVY if not potential else box.ROUNDED,
                 padding=(0, 1))


# ----------------------------------------------------------------- board ----
def _slots() -> list[datetime]:
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    h0, m0 = map(int, DAY_START.split(":"))
    h1, m1 = map(int, DAY_END.split(":"))
    t, end = base.replace(hour=h0, minute=m0), base.replace(hour=h1, minute=m1)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(minutes=SLOT_MIN)
    return out


def _bar(start, end, glyph: str, style: str) -> Text:
    s, e = _dt(start), _dt(end)
    s = s.replace(year=2000, month=1, day=1) + (timedelta(days=1) if s.hour < 6 else timedelta())
    e = e.replace(year=2000, month=1, day=1) + (timedelta(days=1) if e.hour < 6 else timedelta())
    t = Text()
    for slot in _slots():
        se = slot + timedelta(minutes=SLOT_MIN)
        on = s < se and e > slot
        t.append(glyph * 2 if on else "· ", style=style if on else "grey23")
    return t


def render_board(db: DossierDB) -> None:
    tab = Table(box=box.SIMPLE_HEAD, show_lines=False, padding=(0, 1), title="[bold]DOSSIER BOARD[/]  "
                "[grey50]█ presence  ░ absence  ▒ retracted   one cell = 10 min[/]", title_justify="left")
    tab.add_column("", width=34, no_wrap=True)
    tab.add_column(_time_ruler(), no_wrap=True)
    tab.add_column("", no_wrap=True)
    tab.add_row(Text("THE TRUTH  (intel)", style="bold grey70"), "", "")
    for e in db.intel():
        if e["kind"] == "context" or not e.get("start"):
            continue
        place = e.get("place_name")
        glyph = "█" if e["kind"] == "presence" else "░"
        label = Text(f" {e['code']} ", style="bold black on grey70" if e["revealed"] else "bold grey50 on grey15")
        label.append(f" {e['title'][:24]}", style="white" if e["revealed"] else "grey50")
        note = Text(("" if e["kind"] == "presence" else "NOT ") + (place or ""), style=pstyle(place))
        tab.add_row(label, _bar(e["start"], e["end"], glyph, pstyle(place)), note)
    tab.add_row(Text("THE DEBRIEF  (her claims)", style="bold grey70"), "", "")
    for c in db.claims():
        hit = c.get("hits", 0) > 0
        glyph = "▒" if c["retracted"] else "█"
        label = Text(f" T{c['turn']:02d} ", style="bold white on red3" if hit else "bold black on grey50")
        label.append(f" “{c['quote'][:26]}”", style="grey42 strike" if c["retracted"] else ("red1" if hit else "white"))
        note = Text(c["place"], style=pstyle(c["place"]) + (" dim" if c["retracted"] else ""))
        if hit:
            note.append(f"  ✗{c['hits']}", style="bold red1")
        tab.add_row(label, _bar(c["start"], c["end"], glyph, pstyle(c["place"]) + (" dim" if c["retracted"] else "")), note)
    console.print(tab)


def _time_ruler() -> Text:
    r = Text()
    for i, slot in enumerate(_slots()):
        if i % 3 == 0:
            r.append(slot.strftime("%H:%M").ljust(6), style="grey50")
    return r


# ---------------------------------------------------------- inspections ----
def render_intel(db: DossierDB) -> None:
    tab = Table(box=box.SIMPLE_HEAD, title="[bold]INTEL[/]  [grey50](/show CODE to put it on the table)[/]",
                title_justify="left", padding=(0, 1))
    tab.add_column("", width=4)
    tab.add_column("item", width=26)
    tab.add_column("what it says")
    tab.add_column("", width=10)
    for e in db.intel():
        tab.add_row(Text(e["code"], style="bold"), e["title"], Text(e["detail"], style="grey70"),
                    Text("shown" if e["revealed"] else "in folder", style="green3" if e["revealed"] else "grey50"))
    console.print(tab)


def render_flag_list(rows: list[dict]) -> None:
    if not rows:
        console.print(Text("  No flags on the record yet.", style="grey62"))
        return
    for i, c in enumerate(rows, 1):
        console.print(flag_panel(c, i))
    console.print(Text("  /why N  for the full provenance of one flag", style="grey50"))


def render_why(db: DossierDB, c: dict, idx: int) -> None:
    """Click-through: from a flag to the exact words, the turn, and the intel."""
    turn = db.q("SELECT n, question, answer, at FROM $id;", {"id": rid("turn", c["turn"])})[0]
    body = Text()
    body.append("WHAT SHE SAID\n", style="bold")
    body.append(f"  turn {turn['n']} · {hhmm(turn['at'])} real time\n", style="grey50")
    body.append(f"  Q: {turn['question']}\n", style="cyan")
    body.append(f"  A: {turn['answer']}\n", style="white")
    body.append("\nWHAT WAS EXTRACTED\n", style="bold")
    body.append(f"  “{c['quote']}”\n", style="white")
    body.append(f"  → {c['said_place']}  {hhmm(c['s'])}–{hhmm(c['e'])}"
                f"   certainty: {c['certainty']}\n", style=pstyle(c["said_place"]))
    body.append("\nWHAT IT COLLIDES WITH\n", style="bold")
    if c["kind"] == "intel":
        ev = db.q("SELECT *, place.name AS place_name FROM $id;", {"id": rid("intel", c["code"])})[0]
        body.append(f"  {ev['code']} · {ev['title']} · source: {ev['source']} · trust {ev['trust']:.2f}\n", style="white")
        body.append(f"  {ev['detail']}\n", style="grey70")
        body.append(f"  → {'NOT ' if ev['kind'] == 'absence' else ''}{ev.get('place_name')}  "
                    f"{hhmm(ev['start'])}–{hhmm(ev['end'])}\n", style=pstyle(ev.get("place_name")))
    else:
        body.append(f"  her own turn {c['other_turn']}: “{c['other_quote']}”\n", style="white")
    body.append(f"\nOVERLAP  {c['overlap_min']} minutes", style="bold red1")
    body.append("   raised by the raise_flags event inside SurrealDB, in the same transaction as the claim.\n"
                "  No model was asked whether this is a lie.", style="grey50")
    console.print(Panel(body, title=f"[bold]#{idx} PROVENANCE[/]", title_align="left",
                        border_style="red1", box=box.HEAVY, padding=(0, 1)))


def render_blame(db: DossierDB, n: int) -> None:
    rows = db.blame(n)
    if not rows:
        console.print(Text(f"  Turn {n} filed no claims.", style="grey62"))
        return
    for r in rows:
        body = Text()
        body.append(f"Q: {r['question']}\n", style="cyan")
        body.append(f"“{r['quote']}”\n", style="white")
        body.append(f"→ {r['place']} {hhmm(r['start'])}–{hhmm(r['end'])}  "
                    f"(said {hhmm(r['said_at'])} real time, extraction {r['confidence']:.0%})\n", style=pstyle(r["place"]))
        for x in r.get("exposed_by") or []:
            body.append(f"  ✗ [{x['kind']}/{x['certainty']}] {x['reason']} (overlap {x['overlap_min']} min)\n", style="red1")
        for x in r.get("replaced_by") or []:
            body.append(f"  ⤷ retracted at turn {x['turn']}: “{x['quote']}”\n", style="blue")
        for x in r.get("replaces") or []:
            body.append(f"  ⤶ replaced turn {x['turn']}: “{x['quote']}”\n", style="blue")
        console.print(Panel(body, title=f"[bold]BLAME · turn {n}[/]", title_align="left",
                            border_style="grey50", box=box.ROUNDED, padding=(0, 1)))


def render_rewind(db: DossierDB, n: int) -> None:
    """System time: the live story as the database actually held it at the end of turn n."""
    nxt = db.turn_time(n + 1)
    when = nxt if nxt is not None else db.now()
    try:
        then = db.story_at(when)
    except Exception as e:  # noqa: BLE001
        console.print(Text(f"  Time travel needs a versioned store (surrealkv+versioned://). {e}", style="yellow"))
        return
    now = db.claims(live_only=True)
    tab = Table(box=box.SIMPLE_HEAD, padding=(0, 1),
                title=f"[bold]REWIND[/]  [grey50]SELECT … VERSION <end of turn {n}>  vs  now[/]", title_justify="left")
    tab.add_column(f"her story after turn {n}", ratio=1)
    tab.add_column("her story now", ratio=1)
    fmt = lambda c: Text(f"{hhmm(c['start'])}–{hhmm(c['end'])} {c['place']}  “{c['quote'][:38]}”", style=pstyle(c["place"]))
    for i in range(max(len(then), len(now))):
        tab.add_row(fmt(then[i]) if i < len(then) else Text(""), fmt(now[i]) if i < len(now) else Text(""))
    console.print(tab)


def render_memory(db: DossierDB) -> None:
    rows = db.q("SELECT kind, text, turn.n AS turn, at FROM memory ORDER BY at;") or []
    tab = Table(box=box.SIMPLE_HEAD, padding=(0, 1),
                title="[bold]HER MEMORY[/]  [grey50]layer 3: what she recalls, not what is true or what was said[/]",
                title_justify="left")
    tab.add_column("turn", width=5)
    tab.add_column("kind", width=9)
    tab.add_column("memory")
    for r in rows:
        style = {"said": "white", "strategy": "grey62 italic", "learned": "magenta"}.get(r["kind"], "white")
        tab.add_row(str(r.get("turn") or "–"), r["kind"], Text(r["text"], style=style))
    console.print(tab)


def render_sql(db: DossierDB) -> None:
    from .db import SCHEMA
    start = SCHEMA.index("DEFINE EVENT OVERWRITE raise_flags")
    end = SCHEMA.index("-- =============================================================== REPORT")
    from rich.syntax import Syntax
    console.print(Panel(Syntax(SCHEMA[start:end].strip(), "sql", theme="ansi_dark", word_wrap=True),
                        title="[bold]the flags · schema.surql[/]", title_align="left", border_style="grey50"))
    if db.queries:
        console.print(Text("last query run:", style="grey50"))
        console.print(Syntax(db.queries[-1], "sql", theme="ansi_dark", word_wrap=True))


HELP = """[bold]Ask anything in plain words.[/] Commands:
  [cyan]/show E2[/] [grey50][words][/]  put intel on the table (optionally with your own question)
  [cyan]/intel[/]             the intel folder
  [cyan]/board[/]             timeline: the truth vs her debrief
  [cyan]/flags[/]             every flag the database has raised
  [cyan]/why N[/]             provenance of flag N: words, turn, intel, overlap
  [cyan]/blame T[/]           every claim from turn T and the flags it raised
  [cyan]/rewind T[/]          time travel: her story as the database held it after turn T
  [cyan]/memory[/]            the asset's own memory (what she recalls)
  [cyan]/sql[/]               the SurrealQL that flags her
  [cyan]/burn[/]              burn the asset
  [cyan]/quit[/]"""


def render_ending(broke: bool, text: str, st: dict, threshold: int) -> None:
    style = "red1" if broke else "grey62"
    head = "THE ASSET BREAKS" if broke else "COVER HOLDS"
    console.print(Panel(Text(f"“{text}”", style="italic white"), title=f"[bold {style}]{head}[/]",
                        title_align="left", border_style=style, box=box.DOUBLE, padding=(1, 3)))
    console.print(confidence_bar(st, threshold, final=True))
    console.print(Text("  The LLM lied. The database remembered.", style="bold"))
