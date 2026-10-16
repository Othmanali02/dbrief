# Stage notes (3 minutes)

**Before you go up:** `.env` has both keys, `dbrief load` has run, terminal ≥ 120 columns, large font.
Rehearse once live (`dbrief`) and once with `dbrief autopilot --offline --pace 2 --no-step`.
If the wifi dies, run the offline version: the words are scripted, memory (local SurrealDB) and the ledger are real.
A live turn takes about 13 s; fill that time by pointing at what's on screen.

## 0:00 – 0:20 · Hook
> "An agent's memory should know the difference between what you told it and what the records say. DBrief is a claims agent built on SurrealDB Agent Memory, loaded with a real insurance dataset."

Run `dbrief`. Point at the banner: memory = SurrealDB Agent Memory, verification = rules in SurrealDB.

## 0:20 – 1:10 · The near-miss
1. Type: *"Hi, I'm calling about policy 521585. I crashed my Saab around six in the morning."*
   - Saab: **✓ consistent**.
   - Six: **~ near-miss**, record says 5. DBrief asks you to confirm.
   - Point at the **memory line**: Agent Memory recall, hits, cited `policy:521585 (incident_hour_of_the_day = 5)`, the trace id.
   > "The agent never saw that 5 in its prompt. It recalled it from Agent Memory, and a SurrealDB rule decided 6 is a near-miss."
2. Type: *"Five? Yes, that's right, it was five."*
   - Blue **CORRECTED** panel: 6 → 5, consistent. "Retracted, not deleted."

## 1:10 – 1:50 · The receipt
3. `/trace 1`: the Agent Memory trace (query, tier, what it returned), then the cited record, with attribute ids.
   > "Every answer traces back to the SurrealDB record it used. If the agent cites something no recall returned, the database marks it uncited."
4. `/why 1`: the flag → your words → the trace → the record value.

## 1:50 – 2:30 · Conflict and unverified
5. Type: *"It was a rear collision. And it was pouring with rain."*
   - Rear: **✗ conflict** (record: Side Collision). Rain: **? unverified**, since the records have nothing on weather.
6. `/board`: every field, what you said, what the record says, the verdict. `/rewind 1`: the account after turn 1 vs now (`SELECT … VERSION`).

## 2:30 – 3:00 · Close
7. Optional proof: quit, `dbrief --resume`, ask *"What time did I tell you it happened?"*. New process, no history: the answer comes back through Agent Memory, and `/trace` shows the conversation hits.
> "Memory: SurrealDB Agent Memory, authoritative and experiential, every recall traced. Verification: deterministic rules in SurrealDB. The claimant talks; the database checks."

---

## Questions judges will ask

**Where does memory do the work?** The adjuster's prompt has no dataset values. Everything it says about a policy comes from `recall` / `inspect` on Agent Memory, and `/trace` shows the trace and the attribute ids. Run `--no-memory` and every claim comes back unverified, with nothing cited.

**Why not let the LLM judge?** It would guess, and differently each run. Here the claim and the recalled record value go into SurrealDB, and `fn::verdict` decides with fixed tolerances. `/sql` shows the rules.

**Is the dataset real?** Kaggle "Auto Insurance Claims Data", 1,000 policies. We loaded the demo policy plus the next rows. Fields with `?` are left out, which is why some claims are unverified.

**Fraud?** The dataset has a fraud label; we don't load it and don't claim to detect fraud. DBrief checks consistency with the record, nothing more.

Only quote numbers you have measured yourself (see README, "Measured").
