-- Every near-miss and conflict check_claim writes, the moment its transaction commits.
-- Namespace dbrief, database ledger. Start it once a session is running (the table must exist).
LIVE SELECT * FROM contradicts;
