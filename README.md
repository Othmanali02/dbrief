# DBrief

**An insurance claims agent whose memory knows the difference between what you said and what the records say.**

*The claimant talks. The database checks.*

A claimant describes their car incident in plain English. DBrief recalls the policy and incident record from **SurrealDB Agent Memory**, which is loaded with a real dataset (Kaggle, "Auto Insurance Claims Data", `insurance_claims.csv`). It extracts each factual claim from what the claimant said, and a **deterministic rule inside SurrealDB** checks every claim against the recalled record value:

| verdict | when | example (policy 521585) |
|---|---|---|
| ✓ consistent | equal or equivalent | "my Saab" · record `auto_make = Saab` |
| ~ near-miss | close: hour ±1, amount ±10%, count/year ±1, date ±2 days, same street | "around six" · record `incident_hour_of_the_day = 5` |
| ✗ conflict | anything else | "a rear collision" · record `collision_type = Side Collision` |
| ? unverified | the record has no value for it | "it was raining" |

On a near-miss or conflict, the agent tells the claimant what the record shows and asks them to confirm. A confirmation is filed as a revising claim that **supersedes** the original; the old words are retracted, never deleted.

Every answer carries a **receipt**: the Agent Memory trace of each recall, what it retrieved, and which record ids the answer cited. Whether each cited id was actually retrieved is checked by the database, not by a model.

**Memory:** SurrealDB Agent Memory (authoritative + experiential, recall with traces). **Verification:** deterministic rules in SurrealDB.

The agent is meant to be honest and grounded. The claimant may simply be mistaken.

## Quickstart

```bash
pip install -e .
cat > .env <<'EOF'            # git-ignored; never commit it
SPECTRON_API_KEY=...          # SurrealDB Agent Memory (SPECTRON_URL / SPECTRON_CONTEXT_ID have defaults)
ANTHROPIC_API_KEY=...
EOF

dbrief load                   # insurance_claims.csv (DBRIEF_DATA, ./ or ../) -> Agent Memory + local memory
dbrief                        # you are the claimant
dbrief autopilot              # a Claude claimant plays the real policyholder of 521585 (fuzzy memory)
dbrief autopilot --offline    # no network: scripted words, local memory, real ledger (the regression test)
```

Try: *"Hi, I'm calling about policy 521585. I crashed my Saab around six in the morning."*

## How it works

```
claimant text ─► remember()  experiential memory, scope dbrief/claimant/<policy>/<session>
              ─► extractor (Claude): typed claims {field, value, exact quote, confidence}
              ─► adjuster (Claude), tools backed by Agent Memory:
                    recall("policy 521585")  ─► hits + trace.traceId
                    inspect("policy:521585") ─► the record's fields, with attribute ids
                    check_claims(ref)        ─► snapshot the record as memory returned it,
                                                fn::report files each claim ─► check_claim EVENT
                                                (same transaction): consistent / near-miss / conflict / unverified
                    respond(answer, cited_ids)
              ─► trace row: traceIds, retrieved ids, cited ids ─► check_citations EVENT: `uncited`
```

- **The adjuster's prompt contains no dataset values.** It learns about a policy only through `recall` / `inspect`, and must cite what it recalled.
- **The extractor sees field names and category names** (e.g. the four collision types), never a record value. It only files claims whose quote appears in the claimant's own words.
- **The rules** live in `dbrief/ledger.surql` (`fn::norm`, `fn::near`, `fn::verdict`, the `check_claim` event); tolerances in `dbrief/fields.py`. Same claim and record in, same verdict out.
- **The ledger** is a local embedded SurrealDB (`.dbrief-db`, versioned SurrealKV): `claim`, `record` (snapshot + traceId), `contradicts` and `supersedes` edges, `trace`. `/rewind` is `SELECT … VERSION`.

### Agent Memory, as used here

| | call | what DBrief stores |
|---|---|---|
| authoritative | `POST /facts` `infer: "triples"` | one `policy` entity per dataset row, one attribute per checkable field (23), scope `dbrief` |
| experiential | `POST /facts` `infer: "none"` | each claimant turn, scope `dbrief/claimant/<policy>/<session>` |
| recall | `POST /query` (`lens`) | hits + `trace.traceId` |
| inspect | `GET /entities/policy/<n>` | the record's attributes and their ids |
| receipt | `GET /traces/<traceId>` | `retrievals[].returned[]`: ref, label, score, rank |

Differences from the docs we found, where we followed the API: `POST /scopes` takes `{"path": "dbrief"}` and stores `"dbrief/"`; flat `scopes: ["dbrief"]` and `lens: ["dbrief"]` are accepted; query hits carry `resource {entityType, name, key}` but the value only inside `text`; there is no inspect endpoint, `GET /entities/{type}/{name}` does the job.

If `SPECTRON_*` is unset or a call fails, DBrief falls back to **local SurrealDB memory** with the same recall / inspect / remember / trace interface (BM25 over the same attributes) and says so in the banner.

## Commands

| | |
|---|---|
| `/board` | field by field: what the claimant said (and corrected), the record value, the verdict |
| `/flags` | every near-miss and conflict |
| `/why N` | flag N → the claimant's words → the memory trace → the record value and attribute id |
| `/trace T` | the receipt for turn T: each Agent Memory trace, what it returned, what was cited (and whether it was retrieved) |
| `/inspect REF` | read a record from memory, e.g. `/inspect policy:521585` |
| `/blame T` | every claim from turn T, its verdict, what replaced it |
| `/rewind T` | the claimant's account as the ledger held it after turn T, next to now (`VERSION`) |
| `/memory` | what experiential memory holds about this conversation |
| `/sql` | the rules |

## Proof modes

```bash
dbrief --no-memory     # memory tools off: nothing recalled, nothing cited, every claim unverified
dbrief --resume        # new process, no conversation history: earlier turns come back only via Agent Memory (see /trace)
```

## Measured (one laptop, 16 Oct 2026; your numbers will differ)

- Dataset: `insurance_claims.csv`, 1,000 rows × 40 columns. Loaded: the demo policy plus the next rows in file order (250 confirmed in hosted memory at the time of writing), 20–23 attributes each (`?` values are left out). A 2-policy batch (43 triples) took 3.7 s; larger batches ingest much more slowly, so `dbrief load` uses batches of 200 with a 15-minute timeout.
- Hosted recall of a policy: 2.4–2.9 s (`tier: hybrid`); a conversation recall escalated to tier 4 took 7–8 s; `inspect` about 0.4 s.
- A live turn (Claude adjuster + extractor + Agent Memory): about 13 s. The ledger's checks: 2–21 ms per turn.
- Local fallback recall: 0.13–0.2 s.

`fraud_reported` is in the dataset but is **not** loaded into memory, and DBrief does not do fraud detection.

## Database on screen (optional)

The ledger can run on a SurrealDB server so Surrealist can watch it: `surreal start --user root --pass root --bind 127.0.0.1:8000 "surrealkv://.dbrief-server.db?versioned=true"`, then `DBRIEF_DB_USER=root DBRIEF_DB_PASS=root dbrief --db ws://127.0.0.1:8000`. In Surrealist use namespace `dbrief`, database `ledger`, and the queries in `studio/`.

## Layout

```
dbrief/memory.py      AgentMemory (REST) and LocalMemory: recall / inspect / remember / trace / load
dbrief/fields.py      the checkable fields, their rule kinds and tolerances, aliases
dbrief/ledger.surql   the ledger and the rules: fn::verdict, check_claim, fn::report, check_citations
dbrief/ledger.py      ledger access: report, flags, blame, VERSION time travel, trace rows
dbrief/adjuster.py    adjuster (memory tools), extractor, autopilot claimant (Claude)
dbrief/session.py     one turn: remember -> extract -> recall/inspect/check -> respond -> trace
dbrief/offline.py     scripted words for --offline; memory and ledger stay real
dbrief/game.py        CLI: play, autopilot, load
dbrief/ui.py          the terminal
scripts/smoke.py      Agent Memory smoke test (raw responses)
```

## License

MIT. The dataset is not included; `dbrief/data/demo_policy.json` holds the one demo record used offline.
