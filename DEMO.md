# Stage notes (3 minutes)

**Before you go up:** `pip install -e .`, key exported, terminal at least 120 columns, font large.
Rehearse once live and once with `--offline`. If the wifi dies, run `dbrief autopilot --offline`: the asset is scripted, the database is not.

**Database on screen (optional, recommended):** debrief terminal on the left, Surrealist on the right.
1. `surreal start --user root --pass root --bind 127.0.0.1:8000 "surrealkv://.dbrief-server.db?versioned=true"`
2. `DBRIEF_DB_USER=root DBRIEF_DB_PASS=root dbrief --db ws://127.0.0.1:8000`
3. In Surrealist (ns `dbrief`, db `dossier`), run the saved `studio/live_flags.surql`. Start it once the debrief is running, so the table exists; it then keeps working across new debriefs. Keep `studio/graph.surql` in a second tab, result view set to Graph.
4. If the server dies on stage, quit and run `dbrief` without `--db`: the embedded engine takes over with the same rules.

## 0:00 – 0:20 · Hook
> "LLMs can invent a cover story. They can even remember the cover story they invented. I built a debrief where the AI asset is allowed to lie, but she can't hide from the database."

Run `dbrief`. Let them read the dossier for five seconds.

## 0:20 – 1:00 · Pin her down
1. *"Celeste. Walk me through your Tuesday evening, from about eight o'clock."*
   Point at the **filed** lines: her words became rows. Point at the magenta **lead**: the database already flags her against intel you haven't shown her.
2. *"What time exactly did you arrive at The Blue Note?"*
   **✓ consistent with the record.** "It doesn't cry wolf. That part of her story is true."

## 1:00 – 1:40 · The money shot
3. `/show E2`, the safe-house door intercept.
   She shifts her story. Blue **STORY CHANGED** panel: "retracted, not deleted." Then the red flag.
   Glance right: the `discrepancy` rows land in Surrealist's live query as the transaction commits.
   > "I didn't ask another model whether that was a lie. Her claim went into SurrealDB, and an event checked it against the intel and everything she's said, in the same transaction."
4. `/why 1` (or whichever number is the red one in `/flags`): the question, her full answer, the extracted claim, the intel, the overlap in minutes.

## 1:40 – 2:20 · Memory is real
5. `/memory`: "This is all she gets. She never sees the transcript. She looks things up."
6. `/rewind 1`: "Her story after turn one, next to her story now. That's `SELECT … VERSION`, time travel in the storage engine."
7. Optional: quit, `dbrief --resume`, ask about her evening. New process; she remembers from the database alone.

## 2:20 – 3:00 · Close
8. `/sql` for two seconds, then `/burn` once the bar is full (or run `dbrief autopilot` and let the handler agent close it).
> "An agent can keep a history, that history can be challenged, and every flag traces back to the exact words. The LLM lies. The database remembers."

---

## Questions judges will ask

**Why SurrealDB?** The debrief needs a document store for her statements, a graph for discrepancy and supersession edges, full-text search for her recall, events that run the rules inside the write transaction, and time travel for `/rewind`. Here that's one database and one schema file.

**Couldn't an LLM just judge consistency?** It could guess, and the guess would vary between runs. Here claims become rows and the rule is explicit: same asset, different place, overlapping time. Same claims in, same verdict out, and `/why` shows exactly why.

**How do you know she's lying?** We don't call anything a lie. We raise *flags* and label them firm or potential, intel or self-inconsistency. Only intel you've shown her counts toward the dossier. The quote always sits next to the interpretation because extraction can be wrong.

**What's agentic here?** The asset retrieves from her own memory before answering and can call a lookup tool to check what she said. The handler agent reads the database state, including leads, before choosing each move. Remove the memory (`--no-memory`) and the asset is answering blind; `dbrief bench` measures the difference.

**Is this SurrealDB Agent Memory?** No. It's plain SurrealDB, embedded via the Python SDK or as a server. It borrows Agent Memory's ideas (mandatory provenance, non-destructive supersession, uncertainty as a first-class label, authority of intel over chat) and implements them in one open schema.

Only say what you've seen work on your own laptop. Run `dbrief bench --runs 3` before the demo if you want to quote numbers; don't quote numbers you haven't measured.
