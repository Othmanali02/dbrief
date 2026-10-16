# DBrief

**Agent memory that knows the difference between what you said and what the records say, for any dataset.**

*Built on SurrealDB Agent Memory + deterministic checks in SurrealDB.*

Someone tells an agent a story in plain English. DBrief recalls the matching record from **SurrealDB Agent Memory**, extracts each factual claim from what was said, and a **deterministic rule inside SurrealDB** checks every claim against the recalled record value:

| verdict | meaning |
|---|---|
| ✓ consistent | the claim matches the record (exactly, or through an alias) |
| ~ near-miss | close but not equal, within the field type's window or tolerance |
| ✗ conflict | the record says something else |
| ? unverified | the record has no value for it, or it is free text that doesn't match exactly |

On a near-miss or conflict the agent tells the speaker what the record shows and asks them to confirm. A confirmation is filed as a revising claim that **supersedes** the original; the old words are retracted, never deleted.

Every answer carries a **receipt**: the Agent Memory trace of each recall, what it retrieved, and which record ids the answer cited. Whether each cited id was actually retrieved is checked by the database, not by a model.

**Memory:** SurrealDB Agent Memory (authoritative + experiential, recall with traces). **Verification:** deterministic rules in SurrealDB.

## The engine

Everything dataset-specific lives in one file, `domains/<name>.toml`: the CSV, the key column, what a record is called, who is speaking, the agent's persona, and the fields to check, each with a type. The engine (`dbrief/`) names no dataset. The rule for each type is in SurrealDB (`dbrief/ledger.surql`); the domain only supplies parameters:

| type | consistent | near-miss (default) | otherwise |
|---|---|---|---|
| `datetime` | same instant | within `window_hours` (1) | conflict |
| `hour` | same hour | within `window_hours` (1) on a 24-hour clock | conflict |
| `number` | equal | within `pct` (10%) of the record, or `abs` if set | conflict |
| `category` | exact or alias match | none | conflict |
| `place` | exact or alias match | none | conflict |
| `text` | exact match | none | unverified |

A domain can override any parameter for all fields of a type (`[rules]`) or for one field. Same claim and record in, same verdict out.

```
speaker text ─► remember()  experiential memory, scope <domain>/<speaker>/<key>/<session>
             ─► extractor (Claude): typed claims {field, value, exact quote, confidence}
             ─► agent (Claude), tools backed by Agent Memory:
                   recall(...)          ─► hits + trace.traceId
                   inspect(entity:key)  ─► the record's fields, with attribute ids
                   check_claims(ref)    ─► snapshot the record as memory returned it,
                                           fn::report files each claim ─► check_claim EVENT (same transaction)
                   respond(answer, cited_ids)
             ─► trace row: traceIds, retrieved ids, cited ids ─► check_citations EVENT: `uncited`
```

- **The agent's prompt contains no dataset values.** It is a template filled from the domain (persona, entity, speaker, field labels). Everything it says about a record comes from `recall` / `inspect`, and it must cite what it recalled.
- **The extractor sees field names, types and category values**, never a record value, and only files claims whose quote appears in the speaker's own words.
- **The ledger** is a local embedded SurrealDB (`.dbrief-db`, versioned SurrealKV): `claim`, `record` (snapshot + traceId), `contradicts` and `supersedes` edges, `trace`. `/rewind` is `SELECT … VERSION`.
- **Fallback:** if `SPECTRON_*` is unset or a call fails, DBrief uses local SurrealDB memory with the same recall / inspect / remember / trace interface, and the banner says so.

## Quickstart

```bash
pip install -e .
cat > .env <<'EOF'            # git-ignored; never commit it
SPECTRON_API_KEY=...          # SurrealDB Agent Memory (SPECTRON_URL / SPECTRON_CONTEXT_ID have defaults)
ANTHROPIC_API_KEY=...
EOF

dbrief load                   # load the domain's CSV into Agent Memory + local memory
dbrief                        # talk to it
dbrief autopilot              # a Claude speaker plays the real holder of the demo record (fuzzy memory)
dbrief autopilot --offline    # no network: scripted words, local memory, real ledger (the regression test)
```

## Bring your own dataset

```bash
dbrief load my.csv --key id          # profile the CSV, write domains/my.toml, load the rows
dbrief --domain my --key 42          # talk about record 42
```

`dbrief load` guesses a type per column from the values: 0–23 integers in an "hour" column → `hour`, ISO dates → `datetime`, numbers → `number`, city/state/address/zip-like names → `place`, low-cardinality text → `category` with its values, everything else → `text`. The draft is a starting point. Rename labels, delete columns nobody would state, set `persona`, `speaker`, `entity`, `demo_key`, and add aliases. `dbrief load` never overwrites an existing domain file.

We ran the profiler on the insurance CSV (`--local --rows 50`). It gave all 23 fields of the hand-written domain the same type. The hand-written file then sets six parameters the profiler can't know: a 48-hour window for `incident_date`, `abs = 1` for vehicles, injuries, witnesses and car year, and an exact deductible. The profiler also proposed columns nobody would state (premium, capital gains) and the dataset's fraud label, which is why the draft is meant to be edited.

## Example: insurance claims

`domains/insurance.toml`: Kaggle "Auto Insurance Claims Data" (`insurance_claims.csv`, 1,000 rows × 40 columns; not included in the repo). A claimant describes a car incident; the agent is a claims adjuster. 23 checked fields: incident date and hour, city, state, address, incident and collision type, severity, authorities contacted, vehicles, injuries, witnesses, property damage, police report, four claim amounts, car make, model and year, deductible, policy state.

```
Hi, I'm calling about policy 521585. I crashed my Saab around six in the morning.
  ✓ car make Saab              record: Saab
  ~ hour of the incident 6     record: 5        → "Could you confirm the time?"
Five? Yes, that's right, it was five.
  CORRECTED  hour of the incident: 6 → 5 (consistent)   turn 1 claim retracted, not deleted
```

`fraud_reported` is in the dataset but is **not** in the domain file or in memory. DBrief does not do fraud detection.

## Commands

| | |
|---|---|
| `/board` | field by field: what was said (and corrected), the record value, the verdict |
| `/flags` | every near-miss and conflict |
| `/why N` | flag N → the speaker's words → the memory trace → the record value and attribute id |
| `/trace T` | the receipt for turn T: each Agent Memory trace, what it returned, what was cited (and whether it was retrieved) |
| `/inspect REF` | read a record from memory, e.g. `/inspect policy:521585` |
| `/blame T` | every claim from turn T, its verdict, what replaced it |
| `/rewind T` | the account as the ledger held it after turn T, next to now (`VERSION`) |
| `/memory` | what experiential memory holds about this conversation |
| `/sql` | the rules |

Proof modes: `--no-memory` (memory tools off: nothing recalled, nothing cited, every claim unverified) and `--resume` (a new process with no conversation history; earlier turns come back only through Agent Memory, visible in `/trace`).

## Agent Memory, as used here

| | call | what DBrief stores |
|---|---|---|
| authoritative | `POST /facts` `infer: "triples"` | one entity per CSV row (type = the domain's `entity`), one attribute per checked field |
| experiential | `POST /facts` `infer: "none"` | each speaker turn, scope `<domain scope>/<speaker>/<key>/<session>` |
| recall | `POST /query` (`lens`) | hits + `trace.traceId` |
| inspect | `GET /entities/<entity>/<key>` | the record's attributes and their ids |
| receipt | `GET /traces/<traceId>` | `retrievals[].returned[]`: ref, label, score, rank |

Where the API differed from the docs, we followed the API: `POST /scopes` takes `{"path": "..."}` and stores a trailing `/`; flat `scopes: [...]` and `lens: [...]` are accepted; query hits carry `resource {entityType, name, key}` but the value only inside `text`; there is no inspect endpoint, so `GET /entities/{type}/{name}` does the job.

## Measured (one laptop, 16 Oct 2026; your numbers will differ)

- **Hosted recall** of a record: 2.4–3.0 s (`tier: hybrid`). A conversation recall that escalated to tier 4 took 7–8 s. `inspect` about 0.4 s.
- **A live turn** (Claude agent + extractor + Agent Memory): 12–13 s. The ledger's checks: 2–21 ms per turn.
- **Local fallback recall:** 0.12–0.2 s.
- **Hosted ingest is slow** for this context: 2 records (43 triples) took 3.7 s, but 10-record batches (about 220 triples) took 220–270 s each, and 200 records in one request took several minutes. Load once, before you need it. `dbrief load --batch N` sets records per request.

## Database on screen (optional)

The ledger can run on a SurrealDB server so Surrealist can watch it: `surreal start --user root --pass root --bind 127.0.0.1:8000 "surrealkv://.dbrief-server.db?versioned=true"`, then `DBRIEF_DB_USER=root DBRIEF_DB_PASS=root dbrief --db ws://127.0.0.1:8000`. In Surrealist use namespace `dbrief`, database `ledger`, and the queries in `studio/`.

## Layout

```
domains/insurance.toml  the example domain: dataset, key, entity, persona, typed fields, aliases, offline script
dbrief/domain.py        domain loading, CSV profiling, draft domain files
dbrief/memory.py        AgentMemory (REST) and LocalMemory: recall / inspect / remember / trace / load
dbrief/ledger.surql     the ledger and the type rules: fn::near, fn::verdict, check_claim, fn::report, check_citations
dbrief/ledger.py        ledger access: report, flags, blame, VERSION time travel, trace rows
dbrief/agents.py        agent (memory tools), extractor, autopilot speaker: prompt templates filled from the domain
dbrief/session.py       one turn: remember -> extract -> recall/inspect/check -> respond -> trace
dbrief/offline.py       scripted words for --offline, from the domain's [[offline]] beats
dbrief/game.py          CLI: play, autopilot, load
dbrief/ui.py            the terminal
scripts/smoke.py        Agent Memory smoke test (raw responses)
```

## License

MIT. Datasets are not included.
