"""DBrief CLI: talk to the agent, run autopilot, load a dataset into memory."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from rich.text import Text

from . import ui
from .domain import Domain, profile, write_draft
from .ledger import Ledger
from .memory import open_memory
from .session import Session


def build(domain: Domain, live: bool, memory_enabled: bool = True, resume: bool = False,
          db_url: str | None = None, key: str | None = None) -> Session:
    key = key or domain.demo_key
    if not key:
        raise SystemExit(f"domain {domain.name!r} has no demo_key: pass --key <{domain.key}>")
    ui.set_domain(domain)
    memory, note = open_memory(domain, offline=not live)
    ledger = Ledger(domain, db_url, fresh=not resume)
    if live:
        from .agents import Agent, Extractor
        factory = lambda tools: Agent(domain, tools, memory_enabled=memory_enabled)  # noqa: E731
        extractor = Extractor(domain)
    else:
        from .offline import ScriptedAgent, ScriptedExtractor
        factory = lambda tools: ScriptedAgent(domain, tools, key, memory_enabled=memory_enabled)  # noqa: E731
        extractor = ScriptedExtractor(domain)
    return Session(domain, ledger, memory, factory, extractor, key=key, memory_enabled=memory_enabled,
                   memory_note=note)


def mode_label(s: Session, live: bool) -> str:
    return " · ".join(filter(None, [
        "LIVE (Claude)" if live else "OFFLINE (scripted words, real memory and ledger)",
        None if s.memory_enabled else "MEMORY TOOLS OFF",
        s.ledger.url,
    ]))


def turn(s: Session, said: str, who: str | None = None) -> dict:
    ui.render_said(s.ledger.turn_count() + 1, said, who)
    with ui.console.status("[grey50]DBrief is checking the records…[/]", spinner="dots"):
        res = s.ask(said)
    ui.render_turn(res)
    note = getattr(s.memory, "fallback_reason", None)
    if note and not getattr(s, "_warned", False):
        ui.console.print(Text(f"  ⚠ Agent Memory failed, switched to local memory: {note}", style="yellow"))
        s._warned = True
    return res


# ================================================================== play =====
def play(s: Session, live: bool) -> None:
    ui.banner(mode_label(s, live), s.memory.backend, s.memory_note, s.key)
    if s.ledger.turn_count():
        ui.console.print(Text(f"  Resumed: {s.ledger.turn_count()} turns in the ledger. The agent starts with no "
                              f"conversation history: it can only recall the earlier turns through memory.",
                              style="bold magenta"))
    ui.console.print(ui.help_text())
    if s.domain.opening:
        ui.console.print(Text(f"\n  Try: {s.domain.opening}\n", style="grey50"))
    while True:
        try:
            line = ui.console.input(f"[bold cyan]{s.domain.speaker} ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            ui.console.print()
            return
        if not line:
            continue
        if line.startswith("/"):
            if handle_command(s, line):
                return
            continue
        turn(s, line)


def handle_command(s: Session, line: str) -> bool:
    """Returns True to end the session."""
    parts = line.split(maxsplit=1)
    cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    L = s.ledger
    if cmd in ("/quit", "/exit", "/q"):
        ui.render_summary(L.review())
        return True
    if cmd == "/help":
        ui.console.print(ui.help_text())
    elif cmd == "/board":
        ui.render_board(L)
    elif cmd == "/flags":
        ui.render_flag_list(L.all_flags())
    elif cmd == "/why":
        rows = L.all_flags()
        if not arg.isdigit() or not (1 <= int(arg) <= len(rows)):
            ui.console.print(Text(f"  /why 1..{len(rows)}  (see /flags)", style="grey62"))
        else:
            ui.render_why(L, s.memory, rows[int(arg) - 1], int(arg))
    elif cmd == "/trace":
        ui.render_trace(L, s.memory, int(arg) if arg.isdigit() else L.turn_count())
    elif cmd == "/inspect":
        ui.render_inspect(s.memory, arg or s.subject)
    elif cmd == "/blame":
        ui.render_blame(L, int(arg)) if arg.isdigit() else ui.console.print("  /blame TURN")
    elif cmd == "/rewind":
        ui.render_rewind(L, int(arg)) if arg.isdigit() else ui.console.print("  /rewind TURN")
    elif cmd == "/memory":
        ui.render_memory(s.memory, s.key, s.scope)
    elif cmd == "/sql":
        ui.render_sql(L)
    elif cmd == "/review":
        ui.console.print(ui.review_bar(L.review()))
    else:
        ui.console.print(ui.help_text())
    return False


# ============================================================= autopilot =====
def autopilot(s: Session, live: bool, max_turns: int = 8, step: bool = True, pace: float = 0.0) -> dict:
    if live:
        from .agents import SpeakerAgent
        row = s.domain.row(s.key)
        if not row:
            raise SystemExit(f"no row with {s.domain.key} = {s.key} in the dataset")
        speaker = SpeakerAgent(s.domain, row)
    else:
        from .offline import ScriptedSpeaker
        speaker = ScriptedSpeaker(s.domain)
    ui.banner("AUTOPILOT · " + mode_label(s, live), s.memory.backend, s.memory_note, s.key)
    if step:
        ui.console.print(Text(f"  A {s.domain.speaker} agent plays the real record holder. Press Enter to advance.\n",
                              style="grey50"))
    last = ""
    for _ in range(max_turns):
        if step:
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break
        elif pace:
            time.sleep(pace)
        said = speaker.say(last)
        if not said:
            break
        last = turn(s, said, who=f"{s.domain.speaker.upper()} · autopilot")["answer"]
    rv = s.ledger.review()
    ui.render_summary(rv)
    return rv


# ================================================================== load =====
def load(domain_name: str | None, csv_path: str | None, key: str | None, name: str | None, n: int,
         batch: int, local_only: bool) -> None:
    """Load a dataset into memory. With a CSV: profile it and write a draft domain file first."""
    from .memory import AgentMemory, LocalMemory
    if csv_path:
        path = Path(csv_path)
        if not path.exists():
            raise SystemExit(f"no such file: {path}")
        if not key:
            raise SystemExit("dbrief load <csv> needs --key <column>: the column that identifies a record")
        name = name or path.stem.lower().replace(" ", "_").replace("-", "_")
        fields = profile(path, key)
        import csv
        first = next(csv.DictReader(open(path, newline="", encoding="utf-8-sig")), {}).get(key)
        draft = write_draft(name, path, key, fields, first)
        ui.console.print(f"  domain file: {draft}" + ("" if draft.stat().st_mtime > time.time() - 5 else " (exists, kept)"))
        for f, spec in fields.items():
            vals = f"  [{', '.join(spec['values'][:6])}{'…' if len(spec.get('values', [])) > 6 else ''}]" if spec.get("values") else ""
            ui.console.print(Text(f"    {f:32} {spec['type']}{vals}"))
        domain = Domain.load(name)
    else:
        domain = Domain.load(domain_name)
    rows = domain.rows(n)
    ui.console.print(f"  {domain.display}: {domain.csv_path() or 'bundled demo record only'} · {len(rows)} rows · "
                     f"{len(domain.fields)} checked fields · key {domain.key}")
    backend = "local SurrealDB memory"
    if not local_only:
        hosted = AgentMemory.from_env()
        if hosted:
            hosted.timeout = 900
            t0 = time.perf_counter()
            hosted.load(domain, rows, batch=batch, log=ui.console.print)
            ui.console.print(f"  SurrealDB Agent Memory: {len(rows)} rows in {time.perf_counter() - t0:.1f} s "
                             f"(scope {domain.scope})")
            backend = hosted.backend
        else:
            ui.console.print("[yellow]  SPECTRON_* not set: local memory only[/]")
    local = LocalMemory(domain)
    local.db.query("DELETE mem_attr WHERE scope = $s;", {"s": domain.scope + "/"})
    local.load(domain, rows, log=ui.console.print)
    domain.mark_loaded(len(rows), backend)
    ui.console.print(f"  run it: dbrief --domain {domain.name}" + ("" if domain.demo_key else f" --key <{domain.key}>"))


# =================================================================== cli =====
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dbrief", description="Agent memory that knows the difference between what "
                                "you said and what the records say, for any dataset.")
    p.add_argument("mode", nargs="?", default="play", choices=["play", "autopilot", "load", "rehearse"])
    p.add_argument("csv", nargs="?", default=None, help="load: a CSV to profile and load")
    p.add_argument("--domain", default=None, help="domains/<name>.toml (default: DBRIEF_DOMAIN, or the only domain)")
    p.add_argument("--key", default=None, help="load: the CSV's key column; play/autopilot: which record to talk about")
    p.add_argument("--name", default=None, help="load: the domain name (default: the CSV file name)")
    p.add_argument("--offline", action="store_true", help="no network: scripted words, local memory, real ledger")
    p.add_argument("--no-memory", action="store_true", help="proof: memory tools off, nothing can be cited or verified")
    p.add_argument("--resume", action="store_true", help="proof: new process, earlier turns only reachable through memory")
    p.add_argument("--db", default=None, help="ledger SurrealDB URL (default surrealkv+versioned://.dbrief-db)")
    p.add_argument("--turns", type=int, default=8, help="autopilot turn limit")
    p.add_argument("--no-step", action="store_true", help="autopilot runs without waiting for Enter")
    p.add_argument("--pace", type=float, default=0.0, help="autopilot seconds between turns with --no-step")
    p.add_argument("--rows", type=int, default=300, help="load: how many rows (the demo record first)")
    p.add_argument("--batch", type=int, default=50, help="load: rows per Agent Memory request")
    p.add_argument("--local", action="store_true", help="load: local SurrealDB memory only")
    p.add_argument("--record", default=None, help="save the session transcript: name.svg or name.html")
    a = p.parse_args(argv)
    if a.mode == "load":
        load(a.domain, a.csv, a.key, a.name, a.rows, a.batch, a.local)
        return
    if a.mode == "rehearse":
        from .agents import has_claude
        from .memory import AgentMemory
        from .rehearse import main as rehearse
        if not a.offline and not (has_claude() and AgentMemory.from_env()):
            raise SystemExit("live rehearsal needs ANTHROPIC_API_KEY and SPECTRON_API_KEY (or use --offline)")
        rehearse(live=not a.offline)
        return
    if a.record:
        ui.console.record = True
    from .agents import has_claude
    live = not a.offline
    if live and not has_claude():
        ui.console.print("[yellow]No ANTHROPIC_API_KEY: running --offline (scripted words, real memory and ledger).[/]")
        live = False
    domain = Domain.load(a.domain)
    s = build(domain, live, memory_enabled=not a.no_memory, resume=a.resume, db_url=a.db, key=a.key)
    try:
        if a.mode == "autopilot":
            autopilot(s, live, max_turns=a.turns, step=not a.no_step, pace=a.pace)
        else:
            play(s, live)
    finally:
        if a.record:
            save = ui.console.save_html if a.record.endswith(".html") else ui.console.save_svg
            kwargs = {} if a.record.endswith(".html") else {"title": "DBrief · what you said vs what the records say"}
            save(a.record, **kwargs)
            print(f"saved {a.record}")


if __name__ == "__main__":
    main()
