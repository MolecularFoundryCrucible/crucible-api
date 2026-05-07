-- Fix foreign key constraint on scientific_metadata_new.
-- The existing constraint incorrectly points to dataset instead of idtype.
-- This allows scientific metadata to be added to any resource type (samples, projects, etc.)
--
-- Constraint name differs between environments:
--   staging: scientific_metadata_unique_id_fkey
--   main:    scientific_metadata_new_dataset_mfid_fkey
-- Drop whichever exists before running the ADD CONSTRAINT below.

-- For main:
-- ALTER TABLE scientific_metadata_new DROP CONSTRAINT scientific_metadata_new_dataset_mfid_fkey;

-- For staging:
-- ALTER TABLE scientific_metadata_new DROP CONSTRAINT scientific_metadata_unique_id_fkey;

ALTER TABLE scientific_metadata_new
    ADD CONSTRAINT scientific_metadata_new_unique_id_fkey
    FOREIGN KEY (unique_id) REFERENCES idtype(mfid);
