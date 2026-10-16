# Stage notes (3 minutes)

**Before you go up:** `.env` has both keys, `dbrief load` has run (hosted ingest is slow, so do it hours before), terminal ≥ 120 columns, large font.
Rehearse once live (`dbrief`) and once with `dbrief autopilot --offline --pace 2 --no-step`.
If the wifi dies, run the offline version: the words are scripted, but memory (local SurrealDB) and the ledger are real.
A live turn takes about 12–13 s; fill the time by pointing at what's on screen.

## 0:00 – 0:20 · Hook
> "Agents remember what you tell them. DBrief's memory also knows the difference between what you said and what the records say. It works on any dataset; today, a real insurance claims dataset in SurrealDB Agent Memory."

Run `dbrief`. Point at the banner: the domain, the rows loaded, memory = SurrealDB Agent Memory, verification = rules in SurrealDB.

## 0:20 – 1:10 · The near-miss
1. Type: *"Hi, I'm calling about policy 521585. I crashed my Saab around six in the morning."*
   - Saab: **✓ consistent**.
   - Six: **~ near-miss**; the record says 5, and DBrief asks you to confirm.
   - Point at the **memory line**: Agent Memory recall, the hits, the cited `policy:521585`, the trace id.
   > "The agent never saw that 5 in its prompt. It recalled it from Agent Memory, and a SurrealDB rule for the hour type decided that 6 is a near-miss."
2. Type: *"Five? Yes, that's right, it was five."*
   - Blue **CORRECTED** panel: 6 → 5, consistent. "Retracted, not deleted."

## 1:10 – 1:50 · The receipt
3. `/trace 1`: the Agent Memory trace (query, tier, what it returned), then the cited record with attribute ids.
   > "Every answer traces back to the SurrealDB record it used. If the agent cites something no recall returned, the database marks it uncited."
4. `/why 1`: the flag → your words → the trace → the record value.

## 1:50 – 2:30 · Conflict and unverified
5. Type: *"It was a rear collision. And it was pouring with rain."*
   - Rear: **✗ conflict** (record: Side Collision). Rain: **? unverified**: the records have nothing on weather.
6. `/board`: every field, what you said, what the record says, the verdict. `/rewind 1`: the account after turn 1 vs now (`SELECT … VERSION`).

## 2:30 – 3:00 · Close
7. Show `domains/insurance.toml` for five seconds: "This file is the only thing that knows it's insurance. `dbrief load your.csv --key id` writes one for any dataset."
> "Memory: SurrealDB Agent Memory, authoritative and experiential, every recall traced. Verification: deterministic rules in SurrealDB. What you said vs what the records say."

Optional proof if there's time: quit, `dbrief --resume`, ask *"What time did I tell you it happened?"*. It's a new process with no history; the answer comes back through Agent Memory, and `/trace` shows the conversation hits.

---

## Questions judges will ask

**Where does memory do the work?** The agent's prompt has no dataset values. Everything it says about a record comes from `recall` / `inspect` on Agent Memory, and `/trace` shows the trace and the attribute ids. Run `--no-memory` and every claim comes back unverified, with nothing cited.

**Why not let the LLM judge?** It would guess, and differently each run. Here the claim and the recalled record value go into SurrealDB, and `fn::verdict` decides from the field's type with fixed parameters. `/sql` shows the rules.

**Is it really general?** The engine names no dataset: `dbrief/` contains no insurance terms. A domain file sets the dataset, key, entity, persona and typed fields. `dbrief load <csv> --key <col>` profiles a CSV and writes a draft. We demo insurance because that dataset is real.

**Fraud?** The dataset has a fraud label; we don't load it and don't claim to detect fraud. DBrief checks consistency with the record, nothing more.

Only quote numbers you have measured yourself (see the README, "Measured").
