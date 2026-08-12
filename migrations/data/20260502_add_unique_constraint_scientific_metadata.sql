-- Add UNIQUE constraint on unique_id in scientific_metadata_new.
-- Prevents duplicate rows from concurrent add_or_update calls.
--
-- Safe to run on both staging (no duplicates) and main (has duplicates).
-- For duplicates, keeps the row with the highest id (most recently inserted).
-- Run against staging first, then main.

DELETE FROM scientific_metadata_new
WHERE id NOT IN (
    SELECT MAX(id)
    FROM scientific_metadata_new
    GROUP BY unique_id
);

ALTER TABLE scientific_metadata_new
    ADD CONSTRAINT scientific_metadata_new_unique_id_key UNIQUE (unique_id);
