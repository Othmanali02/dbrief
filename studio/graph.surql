-- Her claims, the intel that flags them, the earlier claims they clash with,
-- and the claims they replaced. Switch the result view to "Graph".
SELECT *,
       ->discrepancy->intel AS flagged_by,
       ->discrepancy->claim AS inconsistent_with,
       ->supersedes->claim  AS replaced
FROM claim;
