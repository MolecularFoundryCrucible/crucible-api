-- Benchmark: deletion_status column (idtype) vs EXISTS (deletionrequest)
-- Run each pair in Cloud SQL Studio and compare "Execution Time" at the bottom.
-- Look for: Index Scan (good) vs Seq Scan (no index used).

-- ─────────────────────────────────────────────────────────────
-- DATASETS: current approach (column on idtype)
-- ─────────────────────────────────────────────────────────────
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT dataset.*
FROM dataset
LEFT JOIN idtype ON idtype.mfid = dataset.unique_id
WHERE idtype.deletion_status IS NULL
LIMIT 100;

-- ─────────────────────────────────────────────────────────────
-- DATASETS: proposed approach (EXISTS on deletionrequest)
-- ─────────────────────────────────────────────────────────────
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT dataset.*
FROM dataset
WHERE NOT EXISTS (
    SELECT 1 FROM deletionrequest
    WHERE deletionrequest.resource_id = dataset.unique_id
      AND deletionrequest.status IN ('pending', 'approved')
)
LIMIT 100;


-- ─────────────────────────────────────────────────────────────
-- SAMPLES: current approach (column on idtype)
-- ─────────────────────────────────────────────────────────────
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT sample.*
FROM sample
LEFT JOIN idtype ON idtype.unique_id = sample.unique_id
WHERE idtype.deletion_status IS NULL
LIMIT 100;

-- ─────────────────────────────────────────────────────────────
-- SAMPLES: proposed approach (EXISTS on deletionrequest)
-- ─────────────────────────────────────────────────────────────
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT sample.*
FROM sample
WHERE NOT EXISTS (
    SELECT 1 FROM deletionrequest
    WHERE deletionrequest.resource_id = sample.unique_id
      AND deletionrequest.status IN ('pending', 'approved')
)
LIMIT 100;
