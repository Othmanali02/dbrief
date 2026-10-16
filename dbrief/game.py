"""The debrief: one turn = asset recalls -> answers -> claims extracted -> SurrealDB flags them."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from rich.text import Text

from . import ui
from .db import DossierDB, hhmm

DOSSIERS = Path(__file__).parent / "dossiers"
THRESHOLD = 8


class Debrief:
    def __init__(self, dossier: str = "heron", live: bool = True, memory: bool = True,
                 resume: bool = False, db_url: str | None = None, threshold: int = THRESHOLD):
        self.live = live
        self.memory = memory
        self.threshold = threshold
        embedder = make_embedder() if os.environ.get("DBRIEF_EMBED") else None
        self.db = DossierDB(db_url, fresh=not resume, embedder=embedder)
        path = DOSSIERS / f"{dossier}.json"
        (self.db.attach_dossier if resume else self.db.load_dossier)(path)
        if live:
            from .agents import Asset, Extractor
            self.asset = Asset(self.db, memory=memory)
            self.extractor = Extractor(self.db)
        else:
            from .offline import OfflineAsset
            self.asset = OfflineAsset(self.db, memory=memory)
            self.extractor = None

    @property
    def asset_label(self) -> str:
        a = self.db.dossier["asset"]
        return f"{a['name'].upper()} · {a['codename']}"

    # ------------------------------------------------------------- one turn
    def ask(self, question: str) -> dict:
        n_prior = len(self.db.claims(live_only=True))
        n_intel = len([e for e in self.db.intel() if e["kind"] != "context"])
        said = self.asset.answer(question)
        turn = self.db.add_turn(question, said["answer"])
        n = int(turn.id)
        claims = said["claims"] if "claims" in said else self.extractor.extract(question, said["answer"])
        filed = []
        db_ms = 0.0
        for c in claims:
            t0 = time.perf_counter()
            try:
                self.db.report(turn, c)  # <- raise_flags fires in here
            except Exception as e:  # noqa: BLE001 - one bad extraction must not kill the demo
                ui.console.print(Text(f"  (could not file claim: {e})", style="yellow"))
                continue
            finally:
                db_ms += (time.perf_counter() - t0) * 1000
            place = next((p["name"] for p in self.db.dossier["places"] if p["id"] == c["place"]), c["place"])
            filed.append({**c, "place_name": place})
        if said.get("committed"):
            self.db.remember("said", said["committed"], turn)
        if said.get("strategy"):
            self.db.remember("strategy", said["strategy"], turn)
        new = [c for c in self.db.flags_since(n) if c["turn"] == n]
        revealed = {e["code"]: e["revealed"] for e in self.db.intel()}
        for c in new:
            if c["kind"] == "intel":
                c["revealed"] = revealed.get(c.get("code"), True)
        sup = [s for s in self.db.supersessions() if s["new_turn"] == n]
        return {"n": n, "question": question, "answer": said["answer"], "asset": self.asset_label,
                "recalled": said.get("recalled", []), "lookups": said.get("lookups", []),
                "memory": self.memory, "filed": filed, "flags": new, "superseded": sup,
                "n_prior": n_prior, "n_intel": n_intel, "confidence": self.db.confidence(),
                "db": {"claims": len(filed), "edges": len(new) + len(sup), "ms": db_ms}}

    def reveal(self, code: str, question: str | None = None) -> dict | None:
        ev = self.db.reveal(code)
        if not ev:
            return None
        self.db.remember("learned", f"My handler showed me {ev['title']}: {ev['detail']}")
        return self.ask(question or f"Take a look at this. {ev['title']}: {ev['detail']} Explain that.")

    def burn(self) -> tuple[bool, str]:
        st = self.db.confidence()
        broke = st.get("score", 0) >= self.threshold
        if self.live:
            from .agents import ending
        else:
            from .offline import ending
        return broke, ending(self.db, broke)

    def next_n(self) -> int:
        return self.db.turn_count() + 1


def make_embedder():
    """Optional vectors (DBRIEF_EMBED=1). fastembed downloads a small ONNX model on first use."""
    from fastembed import TextEmbedding
    model = TextEmbedding(os.environ.get("DBRIEF_EMBED_MODEL", "BAAI/bge-small-en-v1.5"))
    return lambda text: [float(x) for x in next(iter(model.embed([text])))]


# ================================================================== play =====
def play(session: Debrief) -> None:
    ui.banner(session.db, mode_label(session))
    if session.db.turn_count():
        ui.console.print(Text(f"  Resumed: {session.db.turn_count()} turns already on the record. "
                              f"She only knows what her memory returns.", style="bold magenta"))
    ui.render_intel(session.db)
    ui.console.print(ui.HELP)
    ui.console.print(Text(f"\n  Try: {session.db.dossier['opening_question']}\n", style="grey50"))
    while True:
        try:
            line = ui.console.input("[bold cyan]handler ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            ui.console.print()
            return
        if not line:
            continue
        if line.startswith("/"):
            if handle_command(session, line):
                return
            continue
        ui.render_question(session.next_n(), line)
        with ui.console.status("[grey50]she is thinking…[/]", spinner="dots"):
            res = session.ask(line)
        ui.render_turn(res, session.threshold)


def handle_command(session: Debrief, line: str) -> bool:
    """Returns True to end the debrief."""
    parts = line.split(maxsplit=2)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    db = session.db
    if cmd in ("/quit", "/exit", "/q"):
        return True
    if cmd == "/help":
        ui.console.print(ui.HELP)
    elif cmd == "/intel":
        ui.render_intel(db)
    elif cmd == "/board":
        ui.render_board(db)
    elif cmd == "/flags":
        ui.render_flag_list(annotate(db, db.all_flags()))
    elif cmd == "/why":
        rows = annotate(db, db.all_flags())
        if not arg.isdigit() or not (1 <= int(arg) <= len(rows)):
            ui.console.print(Text(f"  /why 1..{len(rows)}  (see /flags)", style="grey62"))
        else:
            ui.render_why(db, rows[int(arg) - 1], int(arg))
    elif cmd == "/blame":
        ui.render_blame(db, int(arg)) if arg.isdigit() else ui.console.print("  /blame TURN")
    elif cmd == "/rewind":
        ui.render_rewind(db, int(arg)) if arg.isdigit() else ui.console.print("  /rewind TURN")
    elif cmd == "/memory":
        ui.render_memory(db)
    elif cmd == "/sql":
        ui.render_sql(db)
    elif cmd == "/show":
        code = arg.upper()
        question = parts[2] if len(parts) > 2 else None
        ev = next((e for e in db.intel() if e["code"] == code), None)
        if not ev:
            ui.console.print(Text(f"  No intel {code}. See /intel.", style="yellow"))
        elif ev["revealed"]:
            ui.console.print(Text(f"  She has already seen {code}.", style="yellow"))
        else:
            q = question or f"Take a look at this. {ev['title']}: {ev['detail']} Explain that."
            ui.render_question(session.next_n(), q, who=f"HANDLER · shows {code}")
            with ui.console.status("[grey50]she reads it…[/]", spinner="dots"):
                res = session.reveal(code, q)
            ui.render_turn(res, session.threshold)
    elif cmd == "/burn":
        with ui.console.status("[grey50]…[/]", spinner="dots"):
            broke, text = session.burn()
        ui.render_ending(broke, text, db.confidence(), session.threshold)
        return True
    else:
        ui.console.print(ui.HELP)
    return False


def annotate(db: DossierDB, rows: list[dict]) -> list[dict]:
    revealed = {e["code"]: e["revealed"] for e in db.intel()}
    for c in rows:
        if c["kind"] == "intel":
            c["revealed"] = revealed.get(c.get("code"), True)
    return rows


def mode_label(session: Debrief) -> str:
    return " · ".join(filter(None, [
        "LIVE (Claude)" if session.live else "OFFLINE (scripted asset, real database)",
        None if session.memory else "MEMORY OFF",
        session.db.url,
    ]))


# ============================================================= autopilot =====
def autopilot(session: Debrief, max_turns: int = 10, step: bool = True, pace: float = 0.0) -> dict:
    if session.live:
        from .agents import Handler
    else:
        from .offline import OfflineHandler as Handler
    handler = Handler(session.db, session.threshold)
    ui.banner(session.db, "AUTOPILOT · " + mode_label(session))
    ui.console.print(Text("  The handler agent reads the database before every move. "
                          "Press Enter to advance.\n" if step else "", style="grey50"))
    for i in range(max_turns):
        if step:
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break
        elif pace:
            time.sleep(pace)
        with ui.console.status("[grey50]the handler studies the board…[/]", spinner="dots"):
            move = handler.next_move(max_turns - i)
        if move["action"] == "burn" or i == max_turns - 1:
            ui.render_question(session.next_n(), move.get("question") or "We're done, HERON. You're burned.",
                               who="HANDLER · burns her", target=move.get("target"))
            with ui.console.status("[grey50]…[/]", spinner="dots"):
                broke, text = session.burn()
            ui.render_ending(broke, text, session.db.confidence(), session.threshold)
            return session.db.confidence()
        code = (move.get("intel") or "").upper()
        already = {e["code"]: e["revealed"] for e in session.db.intel()}
        if move["action"] == "reveal" and code in already and not already[code]:
            ui.render_question(session.next_n(), move["question"], who=f"HANDLER · shows {code}", target=move.get("target"))
            with ui.console.status("[grey50]she reads it…[/]", spinner="dots"):
                res = session.reveal(code, move["question"])
        else:
            ui.render_question(session.next_n(), move["question"], target=move.get("target"))
            with ui.console.status("[grey50]she is thinking…[/]", spinner="dots"):
                res = session.ask(move["question"])
        ui.render_turn(res, session.threshold)
    return session.db.confidence()


# ================================================================= bench =====
def bench(runs: int, db_root: str) -> None:
    """Memory ablation: the same fixed debrief, live asset, memory on vs off."""
    from .offline import FIXED_MOVES
    results: dict[str, list[dict]] = {"memory on": [], "memory off": []}
    for label, memory in (("memory on", True), ("memory off", False)):
        for r in range(runs):
            url = f"surrealkv+versioned://{db_root}/bench-{'on' if memory else 'off'}-{r}"
            session = Debrief(live=True, memory=memory, db_url=url)
            for move in FIXED_MOVES:
                if move["action"] == "burn":
                    break
                if move["action"] == "reveal":
                    session.reveal(move["intel"], move["question"])
                else:
                    session.ask(move["question"])
            st = session.db.confidence()
            st["claims"] = len(session.db.claims())
            results[label].append(st)
            ui.console.print(f"  {label} run {r + 1}: self-discrepancies {st['self_discrepancies']}, "
                             f"story changes {st['story_changes']}, intel flags {st['intel_discrepancies']}, "
                             f"claims {st['claims']}")
    tab = ui.Table(box=ui.box.SIMPLE_HEAD, title=f"[bold]MEMORY ABLATION[/] [grey50]{runs} runs each, "
                   f"same {len(FIXED_MOVES) - 1} questions[/]", title_justify="left")
    for col in ("", "self-discrepancies", "story changes", "intel flags", "claims"):
        tab.add_column(col)
    for label, rows in results.items():
        avg = lambda k: sum(x[k] for x in rows) / max(len(rows), 1)
        tab.add_row(label, f"{avg('self_discrepancies'):.1f}", f"{avg('story_changes'):.1f}",
                    f"{avg('intel_discrepancies'):.1f}", f"{avg('claims'):.1f}")
    ui.console.print(tab)
    Path("bench.json").write_text(json.dumps(results, indent=2, default=str))
    ui.console.print(Text("  raw numbers saved to bench.json", style="grey50"))


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
    p = argparse.ArgumentParser(prog="dbrief", description="Debrief an AI asset. The database flags every lie.")
    p.add_argument("mode", nargs="?", default="play", choices=["play", "autopilot", "bench", "load"])
    p.add_argument("--offline", action="store_true", help="scripted asset, no API key needed (database is still real)")
    p.add_argument("--no-memory", action="store_true", help="ablation: the asset gets only the question")
    p.add_argument("--resume", action="store_true", help="reopen the saved debrief and keep going (fresh-session test)")
    p.add_argument("--db", default=None, help="SurrealDB URL (default surrealkv+versioned://.dbrief-db; ws://host:8000 for a server)")
    p.add_argument("--dossier", default="heron")
    p.add_argument("--threshold", type=int, default=THRESHOLD)
    p.add_argument("--turns", type=int, default=10, help="autopilot turn limit")
    p.add_argument("--no-step", action="store_true", help="autopilot runs without waiting for Enter")
    p.add_argument("--pace", type=float, default=0.0, help="autopilot seconds between turns with --no-step")
    p.add_argument("--runs", type=int, default=3, help="bench runs per condition")
    p.add_argument("--record", default=None, help="save the session transcript: name.svg or name.html")
    p.add_argument("--rows", type=int, default=300, help="load: how many dataset rows (the demo policy first)")
    p.add_argument("--local", action="store_true", help="load: local SurrealDB memory only")
    a = p.parse_args(argv)
    if a.mode == "load":
        load_memory(a.rows, local_only=a.local)
        return
    if a.record:
        ui.console.record = True

    live = not a.offline
    if live and not os.environ.get("ANTHROPIC_API_KEY"):
        ui.console.print("[yellow]No ANTHROPIC_API_KEY set. Running --offline (scripted asset, real database).[/]")
        live = False
    if a.mode == "bench":
        if not live:
            sys.exit("bench needs a live asset: set ANTHROPIC_API_KEY.")
        bench(a.runs, ".dbrief-bench")
        return
    session = Debrief(a.dossier, live=live, memory=not a.no_memory, resume=a.resume,
                      db_url=a.db, threshold=a.threshold)
    try:
        if a.mode == "autopilot":
            autopilot(session, max_turns=a.turns, step=not a.no_step, pace=a.pace)
        else:
            play(session)
    finally:
        if a.record:
            save = ui.console.save_html if a.record.endswith(".html") else ui.console.save_svg
            kwargs = {} if a.record.endswith(".html") else {"title": "DBrief · the LLM lies, the database remembers"}
            save(a.record, **kwargs)
            print(f"saved {a.record}")


if __name__ == "__main__":
    main()
