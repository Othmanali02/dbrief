-- Claims, the memory record they were checked against, earlier claims they clash with,
-- and the claims they corrected. Switch the result view to "Graph".
SELECT *,
       ->contradicts->record AS flagged_against,
       ->contradicts->claim  AS clashes_with,
       ->supersedes->claim   AS corrected
FROM claim;
