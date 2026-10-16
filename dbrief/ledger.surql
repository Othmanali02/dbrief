-- ============================================================================
-- DBrief — the claim ledger. The claimant talks; the database checks.
--
-- Memory (what the records say) is SurrealDB Agent Memory. This local ledger
-- holds what the claimant said, one row per typed claim with the exact words,
-- and checks every claim against the record values the adjuster recalled from
-- memory, inside the same transaction that writes the claim. Deterministic
-- rules only: no LLM decides whether a claim matches the record.
--
--   consistent   the claim matches the record (after normalisation)
--   near-miss    close but not equal (hour ±1, amount ±10%, ...)   'potential'
--   conflict     the record says something else                    'firm'
--   unverified   the record has no value for this field
--
-- Three kinds of time:
--   valid time  : claim.start / claim.end      (when it happened, optional)
--   known time  : claim.said_at / turn.n       (when the claimant said it)
--   system time : SELECT ... VERSION <datetime> (what the ledger looked like then)
-- ============================================================================

-- --------------------------------------------------------------- rules -----
DEFINE TABLE OVERWRITE field_rule SCHEMAFULL;
DEFINE FIELD OVERWRITE field ON field_rule TYPE string;
DEFINE FIELD OVERWRITE kind  ON field_rule TYPE string
    ASSERT $value IN ['enum', 'yesno', 'hour', 'count', 'year', 'amount', 'date', 'street'];
DEFINE FIELD OVERWRITE tol   ON field_rule TYPE number;
DEFINE FIELD OVERWRITE label ON field_rule TYPE string;
DEFINE INDEX OVERWRITE field_rule_field ON field_rule FIELDS field UNIQUE;

-- Spoken synonym -> canonical value ("yep" -> YES).
DEFINE TABLE OVERWRITE alias SCHEMAFULL;
DEFINE FIELD OVERWRITE word      ON alias TYPE string;
DEFINE FIELD OVERWRITE canonical ON alias TYPE string;
DEFINE INDEX OVERWRITE alias_word ON alias FIELDS word UNIQUE;

-- -------------------------------------------------------------- record -----
-- A snapshot of one policy record exactly as Agent Memory returned it, with the
-- trace of the recall that surfaced it. Nothing the claimant says edits it.
DEFINE TABLE OVERWRITE record SCHEMAFULL;
DEFINE FIELD OVERWRITE ref      ON record TYPE string;              -- 'policy:521585'
DEFINE FIELD OVERWRITE fields   ON record FLEXIBLE TYPE object;     -- field -> value
DEFINE FIELD OVERWRITE attr_ids ON record FLEXIBLE TYPE object;     -- field -> Agent Memory attribute id
DEFINE FIELD OVERWRITE source   ON record TYPE string;              -- which memory backend
DEFINE FIELD OVERWRITE trace_id ON record TYPE option<string>;      -- the recall that surfaced it
DEFINE FIELD OVERWRITE at       ON record TYPE datetime DEFAULT time::now();
DEFINE INDEX OVERWRITE record_ref ON record FIELDS ref UNIQUE;

-- ------------------------------------------------------------------- meta ---
DEFINE TABLE OVERWRITE meta SCHEMALESS;   -- the conversation's session id (scopes its experiential memory)

-- ------------------------------------------------------------------ turns ---
DEFINE TABLE OVERWRITE turn SCHEMAFULL;
DEFINE FIELD OVERWRITE n        ON turn TYPE int;
DEFINE FIELD OVERWRITE question ON turn TYPE string;   -- what the claimant said
DEFINE FIELD OVERWRITE answer   ON turn TYPE string;   -- what the adjuster replied
DEFINE FIELD OVERWRITE at       ON turn TYPE datetime DEFAULT time::now();
DEFINE INDEX OVERWRITE turn_n   ON turn FIELDS n UNIQUE;

-- ------------------------------------------------------------------ trace ---
-- The receipt for one answer: which Agent Memory traces it used, what those
-- recalls retrieved, and which records the answer cited. `uncited` is filled in
-- by the database: cited ids that no recall in this turn retrieved.
DEFINE TABLE OVERWRITE trace SCHEMAFULL;
DEFINE FIELD OVERWRITE turn      ON trace TYPE record<turn>;
DEFINE FIELD OVERWRITE trace_ids ON trace TYPE array<string>;
DEFINE FIELD OVERWRITE retrieved ON trace TYPE array<string>;
DEFINE FIELD OVERWRITE cited     ON trace TYPE array<string>;
DEFINE FIELD OVERWRITE uncited   ON trace TYPE array<string> DEFAULT [];
DEFINE FIELD OVERWRITE backend   ON trace TYPE string;
DEFINE FIELD OVERWRITE memory_ms ON trace TYPE float DEFAULT 0;
DEFINE EVENT OVERWRITE check_citations ON trace WHEN $event = "CREATE" THEN {
    UPDATE $after.id SET uncited = array::complement($after.cited, $after.retrieved);
};

-- ----------------------------------------------------------------- claims ---
-- One claim = "<field> is <value>", in the claimant's own words.
DEFINE TABLE OVERWRITE claim SCHEMAFULL;
DEFINE FIELD OVERWRITE subject      ON claim TYPE string;              -- 'policy:521585'
DEFINE FIELD OVERWRITE field        ON claim TYPE string;
DEFINE FIELD OVERWRITE value        ON claim TYPE string;
DEFINE FIELD OVERWRITE start        ON claim TYPE option<datetime>;
DEFINE FIELD OVERWRITE end          ON claim TYPE option<datetime>;
DEFINE FIELD OVERWRITE quote        ON claim TYPE string;
DEFINE FIELD OVERWRITE turn         ON claim TYPE record<turn>;
DEFINE FIELD OVERWRITE said_at      ON claim TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE retracted    ON claim TYPE bool DEFAULT false;
DEFINE FIELD OVERWRITE confidence   ON claim TYPE float DEFAULT 1.0;   -- how sure the extractor was
DEFINE FIELD OVERWRITE status       ON claim TYPE option<string>;      -- set by check_claim
DEFINE FIELD OVERWRITE record       ON claim TYPE option<record<record>>;
DEFINE FIELD OVERWRITE record_value ON claim TYPE option<string>;
DEFINE FIELD OVERWRITE trace_id     ON claim TYPE option<string>;      -- the recall it was checked against

-- ------------------------------------------------------------------ edges ---
DEFINE TABLE OVERWRITE contradicts TYPE RELATION IN claim OUT record | claim SCHEMAFULL;
DEFINE FIELD OVERWRITE kind      ON contradicts TYPE string;   -- 'record' | 'self'
DEFINE FIELD OVERWRITE status    ON contradicts TYPE string;   -- 'near-miss' | 'conflict'
DEFINE FIELD OVERWRITE certainty ON contradicts TYPE string;   -- 'potential' | 'firm'
DEFINE FIELD OVERWRITE field     ON contradicts TYPE string;
DEFINE FIELD OVERWRITE claimed   ON contradicts TYPE string;
DEFINE FIELD OVERWRITE expected  ON contradicts TYPE string;   -- the record (or earlier) value
DEFINE FIELD OVERWRITE reason    ON contradicts TYPE string;
DEFINE FIELD OVERWRITE trace_id  ON contradicts TYPE option<string>;
DEFINE FIELD OVERWRITE at        ON contradicts TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE supersedes TYPE RELATION IN claim OUT claim SCHEMAFULL;
DEFINE FIELD OVERWRITE at ON supersedes TYPE datetime DEFAULT time::now();

-- ================================================================ RULES ======
-- Lowercase, single-spaced, '$' and ',' dropped, aliases applied.
DEFINE FUNCTION OVERWRITE fn::norm($v: any) -> string {
    LET $s = array::join(string::words(string::lowercase(string::replace(string::replace(<string> $v, '$', ''), ',', ''))), ' ');
    LET $a = (SELECT VALUE canonical FROM alias WHERE word = $s LIMIT 1)[0];
    RETURN IF $a != NONE THEN string::lowercase($a) ELSE $s END;
};

-- A plain number, decimals allowed ("71610", "71610.0").
DEFINE FUNCTION OVERWRITE fn::isnum($s: string) -> bool {
    LET $parts = string::split($s, '.');
    RETURN array::len($parts) <= 2 AND array::len($parts[WHERE $this = '']) = 0
        AND string::is::numeric(array::join($parts, ''));
};

-- Is a claimed value close to the record value? Pure function of its inputs.
DEFINE FUNCTION OVERWRITE fn::near($kind: string, $tol: number, $a: string, $b: string) -> bool {
    LET $num = fn::isnum($a) AND fn::isnum($b);
    IF $kind = 'hour' AND $num {
        LET $d = math::abs(<int> <float> $a - <int> <float> $b) % 24;
        RETURN math::min([$d, 24 - $d]) <= $tol;
    };
    IF ($kind = 'count' OR $kind = 'year') AND $num {
        RETURN math::abs(<float> $a - <float> $b) <= $tol;
    };
    IF $kind = 'amount' AND $num {
        RETURN math::abs(<float> $a - <float> $b) <= $tol * math::abs(<float> $b);
    };
    IF $kind = 'date' AND string::is::datetime(string::concat($a, 'T00:00:00Z'))
                      AND string::is::datetime(string::concat($b, 'T00:00:00Z')) {
        LET $d = <datetime> string::concat($a, 'T00:00:00Z') - <datetime> string::concat($b, 'T00:00:00Z');
        RETURN math::abs(duration::days($d)) <= $tol;
    };
    IF $kind = 'street' AND array::len(string::words($a)) > 1 {
        RETURN array::join(array::slice(string::words($a), 1), ' ') = array::join(array::slice(string::words($b), 1), ' ');
    };
    RETURN false;
};

-- The verdict for one claimed value against one record value.
DEFINE FUNCTION OVERWRITE fn::verdict($field: string, $claimed: any, $expected: any) -> object {
    LET $rule = (SELECT * FROM field_rule WHERE field = $field LIMIT 1)[0];
    IF $rule = NONE OR $expected = NONE OR $expected = NULL OR <string> $expected = '?' {
        RETURN { status: 'unverified', reason: 'no record value for this field' };
    };
    LET $a = fn::norm($claimed);
    LET $b = fn::norm($expected);
    IF $a = $b { RETURN { status: 'consistent', reason: 'matches the record' }; };
    IF fn::isnum($a) AND fn::isnum($b) AND <float> $a = <float> $b {
        RETURN { status: 'consistent', reason: 'matches the record' };
    };
    IF fn::near($rule.kind, $rule.tol, $a, $b) {
        RETURN { status: 'near-miss', reason: string::concat('close to the record (', $rule.kind, ')') };
    };
    RETURN { status: 'conflict', reason: 'the record says otherwise' };
};

-- ============================================================ THE CHECK ======
-- Fires on every new claim, in the claim's own write transaction.
DEFINE EVENT OVERWRITE check_claim ON claim WHEN $event = "CREATE" THEN {
    LET $c   = $after;
    LET $rec = (SELECT * FROM record WHERE ref = $c.subject LIMIT 1)[0];
    LET $exp = IF $rec != NONE THEN $rec.fields[$c.field] ELSE NONE END;
    LET $v   = fn::verdict($c.field, $c.value, $exp);
    UPDATE $c.id SET
        status       = $v.status,
        record       = $rec.id,
        record_value = IF $exp != NONE THEN <string> $exp ELSE NONE END,
        trace_id     = $rec.trace_id ?? $c.trace_id;

    -- 1. The record says something else.
    IF $v.status IN ['near-miss', 'conflict'] {
        RELATE ($c.id)->contradicts->($rec.id) SET
            kind      = 'record',
            status    = $v.status,
            certainty = IF $v.status = 'conflict' AND $c.confidence >= 0.7 THEN 'firm' ELSE 'potential' END,
            field     = $c.field,
            claimed   = $c.value,
            expected  = <string> $exp,
            reason    = string::concat($v.reason, ': record ', $c.field, ' = ', <string> $exp),
            trace_id  = $rec.trace_id;
    };

    -- 2. The claimant contradicts something they said earlier (live claims only).
    FOR $p IN (SELECT * FROM claim WHERE id != $c.id AND subject = $c.subject
               AND field = $c.field AND retracted = false) {
        IF fn::norm($p.value) != fn::norm($c.value) {
            RELATE ($c.id)->contradicts->($p.id) SET
                kind      = 'self',
                status    = 'conflict',
                certainty = IF $c.confidence >= 0.7 AND $p.confidence >= 0.7 THEN 'firm' ELSE 'potential' END,
                field     = $c.field,
                claimed   = $c.value,
                expected  = $p.value,
                reason    = string::concat('their turn ', <string> $p.turn.n, ': ', $c.field, ' = ', $p.value),
                trace_id  = $rec.trace_id;
        };
    };
};

-- =============================================================== REPORT ======
-- The only way a claim enters the ledger. A revising claim retracts the live
-- claims for the same field (never deletes them) and links them with `supersedes`.
DEFINE FUNCTION OVERWRITE fn::report($turn: record<turn>, $c: object) {
    LET $replaced = IF $c.revises THEN (
        SELECT VALUE id FROM claim WHERE subject = $c.subject AND field = $c.field AND retracted = false
    ) ELSE [] END;
    IF array::len($replaced) > 0 { UPDATE $replaced SET retracted = true; };

    LET $new = CREATE ONLY claim CONTENT {
        subject:    $c.subject,
        field:      $c.field,
        value:      <string> $c.value,
        quote:      $c.quote,
        turn:       $turn,
        confidence: $c.confidence ?? 1.0,
        trace_id:   $c.trace_id,
    };
    FOR $old IN $replaced { RELATE ($new.id)->supersedes->($old); };
    RETURN $new.id;
};

-- =========================================================== THE REVIEW ======
-- The claim-review summary: live claims by status, plus the history.
DEFINE FUNCTION OVERWRITE fn::review() {
    LET $live = (SELECT status FROM claim WHERE retracted = false);
    RETURN {
        consistent:     count($live[WHERE status = 'consistent']),
        near_miss:      count($live[WHERE status = 'near-miss']),
        conflict:       count($live[WHERE status = 'conflict']),
        unverified:     count($live[WHERE status = 'unverified']),
        corrected:      count((SELECT id FROM supersedes)),
        self_conflicts: count((SELECT id FROM contradicts WHERE kind = 'self')),
        flags:          count((SELECT id FROM contradicts)),
    };
};
