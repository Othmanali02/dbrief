# DBrief

**A contradiction engine for agent memory, shown as a debrief.**

*The LLM lies. The database remembers.*

Three of the network's sources were arrested overnight. The only list with their names was in the station's safe house, and someone opened it on Tuesday night. You are the handler. Your asset, HERON, has come in to be debriefed.

The asset is an AI (Claude). She has a secret, a cover story, and a memory she has to manage. Every claim she makes about where she was is filed in SurrealDB with the exact words that made it. A database event checks each new claim against the intel and against everything she has said before, in the same transaction that stores it, and raises a flag on every discrepancy. No model is ever asked whether she is lying.

The more she talks, the more she has to keep straight. When the intel lands, she has three choices: admit the discrepancy, invent another explanation, or contradict herself again. The database records all three.

## Quickstart

```bash
pip install -e .                      # surrealdb, anthropic, rich; SurrealDB runs embedded, no server
export ANTHROPIC_API_KEY=sk-ant-...

dbrief                                # run the debrief yourself
dbrief autopilot                      # watch a handler agent run it (Enter to advance)
dbrief --offline                      # no key: scripted asset, real database
```

Start with *"Celeste. Walk me through your Tuesday evening, from about eight o'clock."* Then `/intel`, and `/show E2` when you're ready.

## How it works: three layers

| Layer | Table | Who writes it | Rule |
|---|---|---|---|
| **The truth** | `intel` | the dossier | Never touched by anything she says. Presence ("the safe-house door camera puts her inside 21:22–21:52"), absence ("the waiter says she was *not* at Lumière after 21:10"), or context. |
| **The debrief** | `claim`, `turn` | the extractor | One row per claim: place, time interval, exact quote, turn, extraction confidence. Never deleted. A changed story *retracts* the old claim and links it with a `supersedes` edge. |
| **Her memory** | `memory` | the asset | What she recalls saying, her private strategy notes, and what she knows the handler has found. She retrieves from it before every answer. She never sees the transcript. |

One turn:

```
question ─► asset recalls from `memory` (full-text, nicknames → places, optional vectors)
         ─► asset answers (Claude), may call recall_memory to check what she said
         ─► extractor (Claude) files claims with a confidence score
         ─► fn::report writes each claim ──► raise_flags EVENT fires, same transaction:
                ├─ claim vs presence intel at a different place   → discrepancy edge
                ├─ claim vs absence intel at the same place       → discrepancy edge
                └─ claim vs her own live claims at a different place → discrepancy edge
         ─► asset writes what she committed to into `memory`
```

### The flags (from `dbrief/schema.surql`)

```sql
DEFINE EVENT raise_flags ON claim WHEN $event = "CREATE" THEN {
    FOR $e IN (SELECT * FROM intel
               WHERE subject = $after.subject AND kind = 'presence' AND place != $after.place) {
        LET $ov = fn::overlap_min($after.start, $after.end, $e.start, $e.end);
        IF $ov > $tol { RELATE ($after.id)->discrepancy->($e.id) SET kind = 'intel', ... };
    };
    -- ... absence intel, and her own earlier claims
};
```

### Three kinds of time

- **Valid time** — `claim.start` / `claim.end`: when she says it happened.
- **Known time** — `claim.said_at` and the turn: when she said it.
- **System time** — `SELECT ... VERSION <datetime>`: her story exactly as the database held it at any moment. `/rewind 1` shows her story after turn 1 next to her story now. This is real time travel in the storage engine, not an app-level history table.

## Commands

| | |
|---|---|
| `/show E2 [words]` | put intel on the table (her memory records that you have it) |
| `/intel` | the intel folder |
| `/board` | timeline: the truth vs her debrief, 10-minute cells |
| `/flags` | every flag on record |
| `/why N` | provenance of one flag: question, full answer, extracted claim, the intel, the overlap |
| `/blame T` | every claim from turn T, what flags it, what replaced it |
| `/rewind T` | time travel: her story after turn T vs now |
| `/memory` | her memory, layer 3 |
| `/sql` | the SurrealQL that flags her |
| `/burn` | burn the asset |

## Scoring rules (visible on screen)

- Overlap alone is not proof. Each flag is labelled **firm** or **potential** (when the extractor was unsure, or the overlap is under 10 minutes), and **intel** or **inconsistency** (with her own words).
- Only intel you have put on the table counts toward the dossier. Flags against intel you haven't shown her appear as **leads**: the database knows something the debrief hasn't used yet.
- Confidence level = 2 per piece of shown intel she firmly contradicts + inconsistencies (max 3) + story changes (max 3). Burn at 8.
- Consistent answers say so: `✓ consistent with the record (checked against 6 intel items and N earlier claims)`.

## What is deterministic, and what isn't

- **LLM:** the asset's answers, claim extraction (its confidence travels with every claim), the handler's choice of move, the prose when she breaks.
- **Database:** every flag, every supersession, the confidence level, recall, time travel. Same claims in, same verdict out.
- Extraction can be wrong. That's why the quote is stored next to the interpretation, `/why` shows both, and shaky readings are labelled potential.

## Options

```bash
dbrief --no-memory          # ablation: she gets only the question, no memory, no lookup tool
dbrief --resume             # reopen the saved debrief in a fresh process; she knows only what her memory returns
dbrief --record demo.svg    # save the session (.svg or .html) for a backup demo
dbrief autopilot --no-step --pace 2
dbrief bench --runs 3       # memory on vs off, same fixed debrief, live asset; writes bench.json
DBRIEF_EMBED=1 dbrief       # add vector recall (pip install -e .[vectors]; downloads a small ONNX model)
```

Models are set with `DBRIEF_ASSET_MODEL`, `DBRIEF_EXTRACT_MODEL` and `DBRIEF_HANDLER_MODEL` (defaults: `claude-sonnet-5-5`, `claude-haiku-4-5`, `claude-sonnet-5-5`). Thinking is switched off on Sonnet 5.5 for stage-speed turns; set `DBRIEF_THINKING=default` to leave it on.

### Database

By default SurrealDB runs embedded through the Python SDK, stored in `.dbrief-db` with a versioned SurrealKV engine (`surrealkv+versioned://`), so time travel works with no server. The SDK's embedded engine is SurrealDB 2.3.

### Server + Surrealist (show the database live)

Run a SurrealDB server and open it in Surrealist next to the debrief. Tested end to end with SurrealDB 3.3.4 and Surrealist 3.9.13.

```bash
curl -sSf https://install.surrealdb.com | sh -s -- --db          # installs to ~/.surrealdb
export PATH=$HOME/.surrealdb:$PATH
surreal start --user root --pass root --bind 127.0.0.1:8000 "surrealkv://.dbrief-server.db?versioned=true"

# second terminal
export DBRIEF_DB_USER=root DBRIEF_DB_PASS=root
dbrief --db ws://127.0.0.1:8000                                  # or: dbrief autopilot --offline --db ws://127.0.0.1:8000
```

The store must be versioned (`?versioned=true` on 3.x, `surrealkv+versioned://` on 2.x) or `/rewind` can't time travel. A new debrief clears the rows but keeps the database, so Studio stays connected between debriefs.

**Surrealist** (desktop app: the AppImage or .deb from [github.com/surrealdb/surrealist/releases](https://github.com/surrealdb/surrealist/releases), or app.surrealdb.com in a browser). New connection: `ws://127.0.0.1:8000`, root / root, namespace `dbrief`, database `dossier`. Two saved queries in `studio/`:

- `studio/live_flags.surql`: `LIVE SELECT * FROM discrepancy;` Each flag appears as `raise_flags` writes it. Start it once a debrief is running, so the table exists.
- `studio/graph.surql`: claims with `->discrepancy->intel`, `->discrepancy->claim` and `->supersedes->claim`. Switch the result view to **Graph**.

SurrealDB 3.x renamed `type::thing` to `type::record`, so the code passes record IDs as parameters and runs on both versions. A 3.3 `VERSION` table scan also returns rows deleted before the timestamp; `/rewind` filters those out.

## Layout

```
dbrief/schema.surql        the three layers, fn::report, the raise_flags event, fn::confidence_level
dbrief/db.py               SurrealDB access: report, recall, flags, blame, VERSION time travel
dbrief/agents.py           asset (with recall_memory tool), extractor, handler, endings (Claude)
dbrief/offline.py          scripted asset and handler: same database, no API calls
dbrief/game.py             the turn pipeline, handler mode, autopilot, bench, CLI
dbrief/ui.py               the terminal workstation
dbrief/dossiers/heron.json the dossier: places with aliases, intel, her truth and cover story
studio/                    saved Surrealist queries
```

## License

MIT
