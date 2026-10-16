-- ============================================================================
-- DBrief — the database is the analyst.
--
-- The asset is an LLM and it will lie. Nothing in this file trusts it.
-- Every statement is stored with its provenance (which turn, which words),
-- and the `raise_flags` event below checks each new claim against intel and
-- against everything the asset has said before — inside the same
-- transaction that writes the claim. No LLM judges anything here.
--
-- Three kinds of time live in this graph:
--   valid time  : claim.start / claim.end      (when she says it happened)
--   known time  : claim.said_at / turn.n       (when she said it)
--   system time : SELECT ... VERSION <datetime> (what the record looked like then)
-- ============================================================================

-- ---------------------------------------------------------------- places ----
-- Entities with several names: "the restaurant", "dinner", "Lumière" are one place.
DEFINE TABLE OVERWRITE place SCHEMAFULL;
DEFINE FIELD OVERWRITE name    ON place TYPE string;
DEFINE FIELD OVERWRITE aliases ON place TYPE array<string>;

-- ------------------------------------------------------------------ intel ---
-- Authoritative facts. presence = she was at `place` during [start, end].
--                      absence  = she was NOT at `place` during [start, end].
--                      context  = narrative only (messages, motive), no location.
DEFINE TABLE OVERWRITE intel SCHEMAFULL;
DEFINE FIELD OVERWRITE code     ON intel TYPE string;
DEFINE FIELD OVERWRITE title    ON intel TYPE string;
DEFINE FIELD OVERWRITE detail   ON intel TYPE string;
DEFINE FIELD OVERWRITE kind     ON intel TYPE string ASSERT $value IN ['presence', 'absence', 'context'];
DEFINE FIELD OVERWRITE subject  ON intel TYPE string;
DEFINE FIELD OVERWRITE place    ON intel TYPE option<record<place>>;
DEFINE FIELD OVERWRITE start    ON intel TYPE option<datetime>;
DEFINE FIELD OVERWRITE end      ON intel TYPE option<datetime>;
DEFINE FIELD OVERWRITE source   ON intel TYPE string;
DEFINE FIELD OVERWRITE trust    ON intel TYPE float ASSERT $value >= 0 AND $value <= 1;
DEFINE FIELD OVERWRITE revealed ON intel TYPE bool DEFAULT false;
DEFINE INDEX OVERWRITE intel_code ON intel FIELDS code UNIQUE;

-- ------------------------------------------------------------------ turns ---
DEFINE TABLE OVERWRITE turn SCHEMAFULL;
DEFINE FIELD OVERWRITE n        ON turn TYPE int;
DEFINE FIELD OVERWRITE question ON turn TYPE string;
DEFINE FIELD OVERWRITE answer   ON turn TYPE string;
DEFINE FIELD OVERWRITE at       ON turn TYPE datetime DEFAULT time::now();
DEFINE INDEX OVERWRITE turn_n   ON turn FIELDS n UNIQUE;

-- ----------------------------------------------------------------- claims ---
-- One claim = "I was at <place> from <start> to <end>", with the exact words.
DEFINE TABLE OVERWRITE claim SCHEMAFULL;
DEFINE FIELD OVERWRITE subject   ON claim TYPE string;
DEFINE FIELD OVERWRITE place     ON claim TYPE record<place>;
DEFINE FIELD OVERWRITE start     ON claim TYPE datetime;
DEFINE FIELD OVERWRITE end       ON claim TYPE datetime;
DEFINE FIELD OVERWRITE activity  ON claim TYPE string;
DEFINE FIELD OVERWRITE quote     ON claim TYPE string;
DEFINE FIELD OVERWRITE turn      ON claim TYPE record<turn>;
DEFINE FIELD OVERWRITE said_at   ON claim TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE retracted ON claim TYPE bool DEFAULT false;
DEFINE FIELD OVERWRITE confidence ON claim TYPE float DEFAULT 1.0;   -- how sure the extractor was
DEFINE FIELD OVERWRITE embedding ON claim TYPE option<array<float>>;

-- -------------------------------------------------------- asset's memory ---
-- Layer 3. Not the truth, not the record: what the asset *remembers*.
--   said     : what she recalls committing to
--   strategy : private notes on how she is playing it
--   learned  : what she knows the handler has found
-- She retrieves from here before every answer instead of rereading the transcript.
-- It can be incomplete. That is the point.
DEFINE TABLE OVERWRITE memory SCHEMAFULL;
DEFINE FIELD OVERWRITE kind      ON memory TYPE string ASSERT $value IN ['said', 'strategy', 'learned'];
DEFINE FIELD OVERWRITE text      ON memory TYPE string;
DEFINE FIELD OVERWRITE turn      ON memory TYPE option<record<turn>>;
DEFINE FIELD OVERWRITE at        ON memory TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE embedding ON memory TYPE option<array<float>>;

-- Full-text recall over her own words (the asset uses this to keep her story straight).
-- The full-text index itself is defined in db.py: the syntax differs between
-- SurrealDB 2.x (SEARCH ANALYZER) and 3.x (FULLTEXT ANALYZER).
DEFINE ANALYZER OVERWRITE statements TOKENIZERS blank, class, punct FILTERS lowercase, ascii, snowball(english);

-- ------------------------------------------------------------------ edges ---
DEFINE TABLE OVERWRITE discrepancy TYPE RELATION IN claim OUT claim | intel SCHEMAFULL;
DEFINE FIELD OVERWRITE kind        ON discrepancy TYPE string;   -- 'intel' | 'self'
DEFINE FIELD OVERWRITE reason      ON discrepancy TYPE string;
DEFINE FIELD OVERWRITE overlap_min ON discrepancy TYPE int;
DEFINE FIELD OVERWRITE weight      ON discrepancy TYPE float;
DEFINE FIELD OVERWRITE certainty   ON discrepancy TYPE string;   -- 'firm' | 'potential'
DEFINE FIELD OVERWRITE at          ON discrepancy TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE supersedes TYPE RELATION IN claim OUT claim SCHEMAFULL;
DEFINE FIELD OVERWRITE at ON supersedes TYPE datetime DEFAULT time::now();

-- ---------------------------------------------------- overlap in minutes ---
-- How many minutes two intervals share (0 if they don't touch).
DEFINE FUNCTION OVERWRITE fn::overlap_min($a_start: datetime, $a_end: datetime, $b_start: datetime, $b_end: datetime) -> int {
    LET $s = IF $a_start > $b_start THEN $a_start ELSE $b_start END;
    LET $e = IF $a_end < $b_end THEN $a_end ELSE $b_end END;
    RETURN IF $e > $s THEN math::floor(duration::mins($e - $s)) ELSE 0 END;
};

-- ============================================================= THE FLAGS ===
-- Fires on every new claim. Same transaction as the write: the lie and its
-- flag are committed together, or not at all.
DEFINE EVENT OVERWRITE raise_flags ON claim WHEN $event = "CREATE" THEN {
    LET $c = $after;
    LET $tol = 4;   -- minutes of slack for travel and fuzzy memory
    -- 'potential' when the extractor was unsure, or the overlap is short (< 10 min)
    LET $sure = IF $c.confidence >= 0.7 THEN 'firm' ELSE 'potential' END;

    -- 1. She says she was somewhere else while intel puts her here.
    FOR $e IN (SELECT * FROM intel
               WHERE subject = $c.subject AND kind = 'presence' AND place != $c.place) {
        LET $ov = fn::overlap_min($c.start, $c.end, $e.start, $e.end);
        IF $ov > $tol {
            RELATE ($c.id)->discrepancy->($e.id) SET
                kind = 'intel',
                certainty = IF $ov < 10 THEN 'potential' ELSE $sure END,
                overlap_min = $ov,
                weight = $e.trust,
                reason = string::concat($e.source, ': ', $e.place.name, ' ',
                    time::format($e.start, '%H:%M'), '–', time::format($e.end, '%H:%M'));
        };
    };

    -- 2. She says she was here while intel says she was not.
    FOR $e IN (SELECT * FROM intel
               WHERE subject = $c.subject AND kind = 'absence' AND place = $c.place) {
        LET $ov = fn::overlap_min($c.start, $c.end, $e.start, $e.end);
        IF $ov > $tol {
            RELATE ($c.id)->discrepancy->($e.id) SET
                kind = 'intel',
                certainty = IF $ov < 10 THEN 'potential' ELSE $sure END,
                overlap_min = $ov,
                weight = $e.trust,
                reason = string::concat($e.source, ': NOT at ', $e.place.name, ' ',
                    time::format($e.start, '%H:%M'), '–', time::format($e.end, '%H:%M'));
        };
    };

    -- 3. She contradicts her own earlier debrief (live claims only).
    FOR $p IN (SELECT * FROM claim
               WHERE id != $c.id AND subject = $c.subject AND retracted = false AND place != $c.place) {
        LET $ov = fn::overlap_min($c.start, $c.end, $p.start, $p.end);
        IF $ov > $tol {
            RELATE ($c.id)->discrepancy->($p.id) SET
                kind = 'self',
                certainty = IF $ov >= 10 AND $c.confidence >= 0.7 AND $p.confidence >= 0.7 THEN 'firm' ELSE 'potential' END,
                overlap_min = $ov,
                weight = 0.6,
                reason = string::concat('her turn ', <string> $p.turn.n, ': ', $p.place.name, ' ',
                    time::format($p.start, '%H:%M'), '–', time::format($p.end, '%H:%M'));
        };
    };
};

-- =============================================================== REPORT ======
-- The only way a claim enters the record. If the asset is revising her story,
-- the claims it replaces are retracted (never deleted) and linked with a
-- `supersedes` edge, so the change of story is itself on the record.
DEFINE FUNCTION OVERWRITE fn::report($turn: record<turn>, $c: object) {
    LET $replaced = IF $c.revises THEN (
        SELECT VALUE id FROM claim
        WHERE subject = $c.subject AND retracted = false AND place != $c.place
          AND fn::overlap_min(start, end, <datetime> $c.start, <datetime> $c.end) > 0
    ) ELSE [] END;
    IF array::len($replaced) > 0 { UPDATE $replaced SET retracted = true; };

    LET $new = CREATE ONLY claim CONTENT {
        subject:   $c.subject,
        place:     $c.place,
        start:     <datetime> $c.start,
        end:       <datetime> $c.end,
        activity:  $c.activity,
        quote:     $c.quote,
        turn:      $turn,
        confidence: $c.confidence ?? 1.0,
        embedding: $c.embedding,
    };
    FOR $old IN $replaced { RELATE ($new.id)->supersedes->($old); };
    RETURN $new.id;
};

-- ======================================================= CONFIDENCE LEVEL ===
-- Only intel put on the table counts toward the dossier. Flags against intel
-- the handler has not shown yet are returned as `leads`: the database knows
-- something the debrief has not used.
DEFINE FUNCTION OVERWRITE fn::confidence_level() {
    LET $n_intel = count((SELECT id FROM discrepancy WHERE kind = 'intel'));
    LET $n_self  = count((SELECT id FROM discrepancy WHERE kind = 'self'));
    LET $n_chg   = count((SELECT id FROM supersedes));
    LET $shown   = array::distinct((SELECT VALUE out FROM discrepancy WHERE kind = 'intel' AND out.revealed = true));
    LET $firm    = array::distinct((SELECT VALUE out FROM discrepancy WHERE kind = 'intel' AND certainty = 'firm' AND out.revealed = true));
    LET $leads   = array::distinct((SELECT VALUE out.code FROM discrepancy WHERE kind = 'intel' AND out.revealed = false));
    RETURN {
        intel_discrepancies: $n_intel,
        self_discrepancies:  $n_self,
        story_changes:       $n_chg,
        intel_hit:           count($shown),
        intel_hit_firm:      count($firm),
        leads:               $leads,
        score: count($firm) * 2 + (count($shown) - count($firm)) + math::min([$n_self, 3]) + math::min([$n_chg, 3]),
    };
};
