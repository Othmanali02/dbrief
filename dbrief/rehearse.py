"""dbrief rehearse: run the stage demo end to end and assert every outcome.

Each step prints PASS or FAIL with the evidence, every turn prints its latency, and any live
turn over 8 s is broken down by call (Claude steps, memory calls, extraction, ledger).
Assertions read the ledger and the memory traces, never the model's wording, except step 2,
which checks that the agent's reply names the record value.

  1  consistent claim: ✓, no flag
  2  near-miss -> the agent offers the record value -> confirmed -> superseded, old claim retracted
  3  hard conflict -> firm flag
  4  /trace on the conflict: traceId present, every cited id retrieved, record re-fetched from memory
  5  --resume in a new process: the earlier turns come back through memory (the trace shows them)
  6  --no-memory: the same statement comes back unverified, with nothing cited
  7  second domain (synthetic CSV): load, one turn, one flag
  8  no Agent Memory key: falls back to local memory and says so; a failing endpoint falls back too
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text

from . import ui
from .domain import DOMAINS, ROOT, STATE, Domain

WORK = ROOT / ".dbrief-rehearse"
SLOW_MS = 8000
SECOND = "rehearse_bookings"
RESUME_Q = "Sorry, I got cut off. Remind me, what time did I tell you it happened?"


class Rehearsal:
    def __init__(self, live: bool):
        self.live = live
        self.results: list[tuple[str, str, bool, str]] = []
        self.turns: list[tuple[str, dict]] = []

    # ------------------------------------------------------------- recording
    def check(self, step: str, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((step, name, bool(ok), detail))
        t = Text(f"  {'PASS' if ok else 'FAIL'}  ", style="bold green3" if ok else "bold red1")
        t.append(f"{step} {name}", style="bold")
        if detail:
            t.append(f"  ·  {detail}", style="grey62")
        ui.console.print(t)
        return ok

    def turn(self, s, said: str, label: str) -> dict:
        from .game import turn
        res = turn(s, said)
        self.turns.append((label, res["timing"]))
        tm = res["timing"]
        line = Text(f"  ⏱ {label}: {tm['total_ms'] / 1000:.1f} s", style="bold cyan")
        line.append(f"  (agent {tm['agent_ms'] / 1000:.1f} s · extract {tm['extract_ms'] / 1000:.1f} s · "
                    f"memory {sum(ms for _, ms in tm['memory_calls'] if 'parallel' not in _) / 1000:.1f} s · "
                    f"ledger {tm['ledger_ms']:.0f} ms)", style="grey62")
        ui.console.print(line)
        return res

    def step(self, title: str) -> None:
        ui.console.print()
        ui.console.rule(Text(f" {title} ", style="bold black on cyan"), style="cyan", align="left")

    # ------------------------------------------------------------------ run
    def run(self) -> bool:
        from .game import build
        WORK.mkdir(exist_ok=True)
        for p in WORK.glob("ledger-*"):
            shutil.rmtree(p, ignore_errors=True)
        d = Domain.load("insurance")
        if len(d.offline) < 4:
            raise SystemExit("the insurance domain needs its [[offline]] beats: they are the stage lines")
        beats = [b["said"] for b in d.offline]
        mode = "LIVE: Claude + SurrealDB Agent Memory" if self.live else "OFFLINE: scripted words, local memory"
        ui.console.print(Text(f"\nDBrief rehearsal · {mode} · domain {d.display} · record {d.ref(d.demo_key)}\n",
                              style="bold"))

        # ---------------------------------------------------- 1-4: the stage path
        s = build(d, self.live, db_url=f"surrealkv+versioned://{WORK}/ledger-a")
        ui.banner(("LIVE" if self.live else "OFFLINE") + " · rehearsal", s.memory.backend, s.memory_note, s.key)
        self.check("0", "memory backend", (s.memory.backend.startswith("SurrealDB Agent Memory")) == self.live,
                   s.memory.backend)

        self.step("1 · consistent claim")
        r1 = self.turn(s, beats[0], "turn 1")
        c1 = [c for c in r1["claims"] if not c["retracted"]]
        self.check("1", "a claim filed and consistent", c1 and all(c["status"] == "consistent" for c in c1),
                   ", ".join(f"{c['field']}={c['value']} {c['status']}" for c in c1) or "no claims")
        self.check("1", "no flag", not r1["flags"], f"{len(r1['flags'])} flags")

        self.step("2 · near-miss -> record value offered -> confirmed -> superseded")
        r2 = self.turn(s, beats[1], "turn 2")
        near = next((c for c in r2["claims"] if c["status"] == "near-miss"), None)
        self.check("2", "near-miss", near, f"{near['field']}: said {near['value']}, record {near['record_value']}" if near
                   else ", ".join(f"{c['field']}={c['value']} {c['status']}" for c in r2["claims"]) or "no claims")
        rec_val = str(near["record_value"]) if near else None
        self.check("2", "agent offers the record value", near and rec_val in r2["answer"],
                   f"record value {rec_val!r} {'in' if near and rec_val in r2['answer'] else 'NOT in'} the reply")
        r3 = self.turn(s, beats[2], "turn 3")
        sup = r3["superseded"]
        self.check("2", "confirmation supersedes the near-miss",
                   near and any(x["field"] == near["field"] and x["old_value"] == near["value"] and
                                x["new_status"] == "consistent" for x in sup),
                   "; ".join(f"{x['field']}: {x['old_value']} -> {x['new_value']} ({x['new_status']})" for x in sup) or "nothing superseded")
        old = [c for c in s.ledger.claims() if near and c["id"] == near["id"]]
        self.check("2", "old claim retracted, not deleted", old and old[0]["retracted"],
                   "still in the ledger, retracted = true" if old and old[0]["retracted"] else "missing or live")

        self.step("3 · hard conflict")
        r4 = self.turn(s, beats[3], "turn 4")
        conflict = next((f for f in r4["flags"] if f["kind"] == "record" and f["status"] == "conflict"), None)
        self.check("3", "firm conflict flag", conflict and conflict["certainty"] == "firm",
                   f"{conflict['field']}: said {conflict['claimed']}, record {conflict['expected']} ({conflict['certainty']})"
                   if conflict else f"{len(r4['flags'])} flags")

        self.step("4 · /trace on the conflict")
        n4 = r4["n"]
        ui.render_trace(s.ledger, s.memory, n4)
        tr = s.ledger.trace_for(n4) or {}
        self.check("4", "Agent Memory traceId present" if self.live else "memory traceId present",
                   bool(tr.get("trace_ids")), ", ".join(tr.get("trace_ids") or []) or "none")
        self.check("4", "every cited id was retrieved", tr.get("cited") and not tr.get("uncited"),
                   f"cited {tr.get('cited')} · uncited {tr.get('uncited')}")
        if conflict:
            t0 = time.perf_counter()
            again = s.memory.inspect(conflict["ref"])
            got = again["fields"].get(conflict["field"])
            self.check("4", "record re-fetched from memory", str(got) == str(conflict["expected"]),
                       f"{conflict['ref']} {conflict['field']} = {got} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
            tid = conflict.get("trace_id")
            try:
                t = s.memory.trace(tid) if tid else None
                self.check("4", "the flag's trace resolves", bool(t and t["returned"]),
                           f"{tid}: {len(t['returned'])} returned, query “{t['query']}”" if t else "no trace id on the flag")
            except Exception as e:  # noqa: BLE001
                self.check("4", "the flag's trace resolves", False, str(e))
        if len(beats) > 4:
            self.turn(s, beats[4], "turn 5")
        s.close()

        # ---------------------------------------------------- 5: --resume
        self.step("5 · --resume in a new process")
        env = dict(os.environ)
        args = [sys.executable, "-c", "import sys; from dbrief.rehearse import resume_probe; "
                f"resume_probe({str(WORK / 'ledger-a')!r}, {self.live!r}, sys.argv[1])", RESUME_Q]
        t0 = time.perf_counter()
        proc = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, env=env, timeout=600)
        wall = (time.perf_counter() - t0) * 1000
        out = next((line for line in proc.stdout.splitlines()[::-1] if line.startswith("{")), None)
        if proc.returncode or not out:
            self.check("5", "new process ran", False, (proc.stderr or proc.stdout)[-400:])
        else:
            r = json.loads(out)
            self.turns.append(("resume turn (new process)", r["timing"]))
            ui.console.print(Text(f"  {d.speaker.upper()}  {RESUME_Q}", style="cyan"))
            ui.console.print(Text(f"  DBRIEF  {r['answer']}", style="white"))
            ui.console.print(Text(f"  ⏱ resume turn: {r['timing']['total_ms'] / 1000:.1f} s in-process, "
                                  f"{wall / 1000:.1f} s with process start", style="bold cyan"))
            self.check("5", "fresh process, no conversation history", r["history"] == 0 and r["ledger_turns"] >= 4,
                       f"in-process history {r['history']}, ledger turns {r['ledger_turns']}")
            conv = [x for x in r["recalls"] if x["scope"] == "conversation"]
            earlier = [h for x in conv for h in x["hits"] if any(b[:20].lower() in h.lower() for b in beats)]
            self.check("5", "earlier turns recalled through memory", bool(earlier),
                       f"{len(earlier)} conversation hits, e.g. “{earlier[0][:70]}”" if earlier else
                       f"conversation recalls: {len(conv)}, hits: {sum(len(x['hits']) for x in conv)}")
            self.check("5", "the trace shows it", any(x["trace_id"] for x in conv),
                       ", ".join(x["trace_id"] for x in conv if x["trace_id"]) or "no trace id")

        # ---------------------------------------------------- 6: --no-memory
        self.step("6 · --no-memory: the same statement")
        s6 = build(d, self.live, memory_enabled=False, db_url=f"surrealkv+versioned://{WORK}/ledger-b")
        r6 = self.turn(s6, beats[1], "no-memory turn")
        self.check("6", "comes back unverified", r6["claims"] and all(c["status"] == "unverified" for c in r6["claims"]),
                   ", ".join(f"{c['field']}={c['value']} {c['status']}" for c in r6["claims"]) or "no claims")
        self.check("6", "no citations, no recall", not r6["cited"] and not r6["recalls"],
                   f"cited {r6['cited']} · recalls {len(r6['recalls'])}")
        s6.close()

        # ---------------------------------------------------- 7: second domain
        self.step("7 · second domain: load, one turn, one flag")
        self.second_domain()

        # ---------------------------------------------------- 8: fallback
        self.step("8 · no Agent Memory key / failing endpoint")
        self.fallback(d)

        return self.summary()

    # ------------------------------------------------------------ step 7
    def second_domain(self) -> None:
        from .game import build, load
        csv_path = WORK / "bookings_synthetic.csv"
        rows = synthetic_bookings()
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        toml = DOMAINS / f"{SECOND}.toml"
        toml.unlink(missing_ok=True)
        t0 = time.perf_counter()
        try:
            load(None, str(csv_path), "booking_id", SECOND, 3 if self.live else len(rows), 3, local_only=not self.live)
        except Exception as e:  # noqa: BLE001
            self.check("7", "load", False, str(e)[:200])
            return
        load_ms = (time.perf_counter() - t0) * 1000
        d2 = Domain.load(SECOND)
        self.check("7", "domain file drafted and rows loaded", toml.exists() and d2.loaded().get("rows"),
                   f"{len(d2.fields)} typed fields: " + ", ".join(f"{k}:{v['type']}" for k, v in d2.fields.items())
                   + f" · {d2.loaded().get('rows')} rows into {d2.loaded().get('backend')} in {load_ms / 1000:.1f} s")
        said = "Hi, this is about booking B001. I stayed five nights, in a double room."
        with open(toml, "a") as f:  # the scripted words for --offline; the live extractor ignores them
            f.write('\n[[offline]]\nsaid = ' + json.dumps(said) + '\nclaims = [{ field = "nights", value = "5", '
                    'quote = "I stayed five nights" }, { field = "room_type", value = "Double", quote = "in a double room" }]\n')
        d2 = Domain.load(SECOND)
        s7 = build(d2, self.live, db_url=f"surrealkv+versioned://{WORK}/ledger-c", key="B001")
        r7 = self.turn(s7, said, "second-domain turn")
        self.check("7", "one turn, at least one flag", bool(r7["flags"]),
                   "; ".join(f"{f['field']}: said {f['claimed']}, record {f['expected']} ({f['status']})" for f in r7["flags"])
                   or ", ".join(f"{c['field']}={c['value']} {c['status']}" for c in r7["claims"]) or "no claims")
        s7.close()
        toml.unlink(missing_ok=True)  # leave no trace in domains/
        try:
            state = json.loads(STATE.read_text())
            state.pop(SECOND, None)
            STATE.write_text(json.dumps(state, indent=2))
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ step 8
    def fallback(self, d: Domain) -> None:
        from .memory import AgentMemory, FallbackMemory, LocalMemory, open_memory
        saved = os.environ.get("SPECTRON_API_KEY")
        os.environ["SPECTRON_API_KEY"] = ""
        try:
            mem, note = open_memory(d, offline=False)
            self.check("8", "key unset: local memory, banner says so",
                       isinstance(mem, LocalMemory) and note and "SPECTRON" in note, note or "no note")
            with ui.console.capture() as cap:
                ui.banner("LIVE · rehearsal", mem.backend, note, d.demo_key)
            self.check("8", "banner text", "SPECTRON_* not set" in cap.get(), "⚠ " + (note or ""))
            mem.db.close()
        finally:
            if saved is None:
                os.environ.pop("SPECTRON_API_KEY", None)
            else:
                os.environ["SPECTRON_API_KEY"] = saved
        bad = AgentMemory("https://127.0.0.1:9", "nope", "not-a-key", timeout=3)
        fb = FallbackMemory(bad, lambda: LocalMemory(d))
        try:
            r = fb.recall(f"{d.entity} {d.demo_key}", scopes=[d.scope])
            self.check("8", "failing endpoint: falls back, no crash", fb.local is not None and r["hits"],
                       f"{fb.backend} · {len(r['hits'])} hits · reason: {(fb.fallback_reason or '')[:60]}")
        except Exception as e:  # noqa: BLE001
            self.check("8", "failing endpoint: falls back, no crash", False, str(e)[:200])
        finally:
            if fb.local:
                fb.local.db.close()
        if self.live:  # the real CLI, live Claude, no Agent Memory key
            env = {**os.environ, "SPECTRON_API_KEY": "", "COLUMNS": "140"}
            proc = subprocess.run([sys.executable, "-m", "dbrief", "autopilot", "--no-step", "--turns", "1",
                                   "--db", f"surrealkv+versioned://{WORK}/ledger-d"],
                                  capture_output=True, text=True, cwd=ROOT, env=env, timeout=600)
            self.check("8", "CLI with the key unset runs and says so",
                       proc.returncode == 0 and "SPECTRON_* not set" in proc.stdout,
                       f"exit {proc.returncode}" + ("" if proc.returncode == 0 else f": {proc.stderr[-200:]}"))

    # ------------------------------------------------------------ summary
    def summary(self) -> bool:
        ui.console.print()
        tab = Table(title="[bold]REHEARSAL[/]", title_justify="left", show_header=True, header_style="bold")
        for col in ("step", "check", "result", "evidence"):
            tab.add_column(col)
        for step, name, ok, detail in self.results:
            tab.add_row(step, name, Text("PASS" if ok else "FAIL", style="bold green3" if ok else "bold red1"),
                        Text(detail[:90], style="grey62"))
        ui.console.print(tab)
        lat = Table(title="[bold]TURN LATENCY[/]", title_justify="left", header_style="bold")
        for col in ("turn", "total", "agent", "extract", "memory", "ledger"):
            lat.add_column(col, justify="right" if col != "turn" else "left")
        for label, tm in self.turns:
            slow = tm["total_ms"] > SLOW_MS
            lat.add_row(label, Text(f"{tm['total_ms'] / 1000:.1f} s", style="bold red1" if slow else "white"),
                        f"{tm['agent_ms'] / 1000:.1f} s", f"{tm['extract_ms'] / 1000:.1f} s",
                        f"{sum(ms for n, ms in tm['memory_calls'] if 'parallel' not in n) / 1000:.1f} s",
                        f"{tm['ledger_ms']:.0f} ms")
        ui.console.print(lat)
        slow = [(label, tm) for label, tm in self.turns if tm["total_ms"] > SLOW_MS]
        for label, tm in slow:
            calls = sorted([*tm.get("llm_calls", []), *tm["memory_calls"], ("claude.extract (parallel)", tm["extract_ms"])],
                           key=lambda x: -x[1])
            ui.console.print(Text(f"  over {SLOW_MS / 1000:.0f} s: {label} ({tm['total_ms'] / 1000:.1f} s). Slowest calls:",
                                  style="bold yellow"))
            for name, ms in calls[:5]:
                ui.console.print(Text(f"    {ms / 1000:6.1f} s  {name}", style="yellow"))
        passed = sum(ok for *_, ok, _ in self.results)
        ok = passed == len(self.results)
        ui.console.print(Text(f"\n  {passed}/{len(self.results)} checks passed · {len(self.turns)} turns · "
                              f"{len(slow)} over {SLOW_MS / 1000:.0f} s", style="bold green3" if ok else "bold red1"))
        return ok


def synthetic_bookings(n: int = 12) -> list[dict]:
    """A small deterministic, synthetic dataset (not real data) for the second-domain check."""
    cities = ["Lisbon", "Porto", "Madrid", "Seville", "Valencia", "Bilbao"]
    rooms = ["Single", "Double", "Suite"]
    out = []
    for i in range(n):
        nights = 2 + (i * 3) % 5
        out.append({"booking_id": f"B{i + 1:03d}", "city": cities[i % len(cities)],
                    "check_in": f"2026-03-{2 + i:02d} {14 + i % 4}:00", "nights": str(nights),
                    "room_type": rooms[(i + 1) % len(rooms)], "total_price": str(nights * (90 + 15 * (i % 3)))})
    return out


def resume_probe(ledger_dir: str, live: bool, question: str) -> None:
    """Run in a separate process by step 5: reopen the ledger, ask once, print the result as JSON."""
    from .game import build
    ui.console.quiet = True
    d = Domain.load("insurance")
    s = build(d, live, resume=True, db_url=f"surrealkv+versioned://{ledger_dir}")
    history, turns = len(s.history), s.ledger.turn_count()
    r = s.ask(question)
    print(json.dumps({"history": history, "ledger_turns": turns, "answer": r["answer"], "cited": r["cited"],
                      "recalls": [{"scope": x["scope"], "trace_id": x.get("trace_id"),
                                   "hits": [h["text"] for h in x["hits"]]} for x in r["recalls"]],
                      "timing": r["timing"]}, default=str))
    s.close()


def main(live: bool) -> None:
    ok = Rehearsal(live).run()
    sys.exit(0 if ok else 1)
