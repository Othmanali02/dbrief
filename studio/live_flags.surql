-- Every flag raise_flags writes, the moment its transaction commits.
-- Start it once a debrief is running (the table must exist). It keeps working across new debriefs.
LIVE SELECT * FROM discrepancy;
