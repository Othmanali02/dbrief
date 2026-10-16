"""DBrief: the claims desk. Player mode, autopilot, loading memory, and the CLI."""
from __future__ import annotations

import argparse
import json
import os
import time

from rich.text import Text

from . import ui
from .ledger import Ledger
from .memory import DEMO_POLICY, open_memory
from .session import Session


def build(live: bool, memory_enabled: bool = True, resume: bool = False, db_url: str | None = None,
          policy: str = DEMO_POLICY) -> Session:
    memory, note = open_memory(offline=not live)
    ledger = Ledger(db_url, fresh=not resume)
    if not resume and hasattr(memory, "db"):  # local memory: start the conversation scope clean
        memory.db.query("DELETE mem_note;")
    if live:
        from .adjuster import Adjuster, Extractor
        factory = lambda tools: Adjuster(tools, memory_enabled=memory_enabled)  # noqa: E731
        extractor = Extractor()
    else:
        from .offline import ScriptedAdjuster, ScriptedExtractor
        factory = lambda tools: ScriptedAdjuster(tools, policy, memory_enabled=memory_enabled)  # noqa: E731
        extractor = ScriptedExtractor()
    return Session(ledger, memory, factory, extractor, policy=policy, memory_enabled=memory_enabled,
                   memory_note=note)


def mode_label(s: Session, live: bool) -> str:
    return " · ".join(filter(None, [
        "LIVE (Claude)" if live else "OFFLINE (scripted words, real memory and ledger)",
        None if s.memory_enabled else "MEMORY TOOLS OFF",
        s.ledger.url,
    ]))


def turn(s: Session, said: str, who: str = "CLAIMANT") -> dict:
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
    ui.banner(mode_label(s, live), s.memory.backend, s.memory_note, s.policy)
    if s.ledger.turn_count():
        ui.console.print(Text(f"  Resumed: {s.ledger.turn_count()} turns in the ledger. The adjuster starts with no "
                              f"conversation history: it can only recall the earlier turns through memory.",
                              style="bold magenta"))
    ui.console.print(ui.HELP)
    ui.console.print(Text(f"\n  Try: Hi, I'm calling about policy {s.policy}. I crashed my Saab around six in the morning.\n",
                          style="grey50"))
    while True:
        try:
            line = ui.console.input("[bold cyan]claimant ›[/] ").strip()
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
        ui.console.print(ui.HELP)
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
        n = int(arg) if arg.isdigit() else L.turn_count()
        ui.render_trace(L, s.memory, n)
    elif cmd == "/inspect":
        ui.render_inspect(s.memory, arg or f"policy:{s.policy}")
    elif cmd == "/blame":
        ui.render_blame(L, int(arg)) if arg.isdigit() else ui.console.print("  /blame TURN")
    elif cmd == "/rewind":
        ui.render_rewind(L, int(arg)) if arg.isdigit() else ui.console.print("  /rewind TURN")
    elif cmd == "/memory":
        ui.render_memory(s.memory, s.policy)
    elif cmd == "/sql":
        ui.render_sql(L)
    elif cmd == "/review":
        ui.console.print(ui.review_bar(L.review()))
    else:
        ui.console.print(ui.HELP)
    return False


# ============================================================= autopilot =====
def autopilot(s: Session, live: bool, max_turns: int = 8, step: bool = True, pace: float = 0.0) -> dict:
    if live:
        from .adjuster import ClaimantAgent
        from .memory import demo_rows
        row = next(r for r in demo_rows() if r["policy_number"] == s.policy)
        claimant = ClaimantAgent(row)
    else:
        from .offline import ScriptedClaimant
        claimant = ScriptedClaimant()
    ui.banner("AUTOPILOT · " + mode_label(s, live), s.memory.backend, s.memory_note, s.policy)
    if step:
        ui.console.print(Text("  A claimant agent plays the real policyholder. Press Enter to advance.\n", style="grey50"))
    last = ""
    for _ in range(max_turns):
        if step:
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break
        elif pace:
            time.sleep(pace)
        said = claimant.say(last)
        if not said:
            break
        last = turn(s, said, who="CLAIMANT · autopilot")["answer"]
    rv = s.ledger.review()
    ui.render_summary(rv)
    return rv


# ================================================================== load =====
def load_memory(n: int, local_only: bool = False) -> None:
    """Authoritative memory: one `policy` entity per dataset row, one attribute per checkable field."""
    from .memory import AgentMemory, LocalMemory, dataset_path, demo_rows
    rows = demo_rows(n)
    ui.console.print(f"  dataset: {dataset_path() or 'bundled demo policy only'} · {len(rows)} policies")
    if not local_only:
        hosted = AgentMemory.from_env()
        if hosted:
            hosted.timeout = 900
            t0 = time.perf_counter()
            hosted.load(rows, log=ui.console.print)
            ui.console.print(f"  SurrealDB Agent Memory: {len(rows)} policies in {time.perf_counter() - t0:.1f} s")
        else:
            ui.console.print("[yellow]  SPECTRON_* not set: skipping hosted Agent Memory[/]")
    local = LocalMemory()
    local.db.query("DELETE mem_attr;")
    local.load(rows, log=ui.console.print)


# =================================================================== cli =====
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dbrief", description="An insurance claims agent whose memory knows the "
                                "difference between what you said and what the records say.")
    p.add_argument("mode", nargs="?", default="play", choices=["play", "autopilot", "load"])
    p.add_argument("--offline", action="store_true", help="no network: scripted words, local memory, real ledger")
    p.add_argument("--no-memory", action="store_true", help="proof: memory tools off, nothing can be cited or verified")
    p.add_argument("--resume", action="store_true", help="proof: new process, earlier turns only reachable through memory")
    p.add_argument("--policy", default=DEMO_POLICY)
    p.add_argument("--db", default=None, help="ledger SurrealDB URL (default surrealkv+versioned://.dbrief-db)")
    p.add_argument("--turns", type=int, default=8, help="autopilot turn limit")
    p.add_argument("--no-step", action="store_true", help="autopilot runs without waiting for Enter")
    p.add_argument("--pace", type=float, default=0.0, help="autopilot seconds between turns with --no-step")
    p.add_argument("--rows", type=int, default=300, help="load: how many dataset rows (the demo policy first)")
    p.add_argument("--local", action="store_true", help="load: local SurrealDB memory only")
    p.add_argument("--record", default=None, help="save the session transcript: name.svg or name.html")
    a = p.parse_args(argv)
    if a.mode == "load":
        load_memory(a.rows, local_only=a.local)
        return
    if a.record:
        ui.console.record = True
    from .adjuster import has_claude
    live = not a.offline
    if live and not has_claude():
        ui.console.print("[yellow]No ANTHROPIC_API_KEY: running --offline (scripted words, real memory and ledger).[/]")
        live = False
    s = build(live, memory_enabled=not a.no_memory, resume=a.resume, db_url=a.db, policy=a.policy)
    try:
        if a.mode == "autopilot":
            autopilot(s, live, max_turns=a.turns, step=not a.no_step, pace=a.pace)
        else:
            play(s, live)
    finally:
        if a.record:
            save = ui.console.save_html if a.record.endswith(".html") else ui.console.save_svg
            kwargs = {} if a.record.endswith(".html") else {"title": "DBrief · the claimant talks, the database checks"}
            save(a.record, **kwargs)
            print(f"saved {a.record}")


if __name__ == "__main__":
    main()
