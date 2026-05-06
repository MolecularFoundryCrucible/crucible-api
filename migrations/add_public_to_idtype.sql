-- Add public column to idtype (Resource) table.
-- Backfills from dataset.public for existing dataset resources.
-- All other resource types default to FALSE.
-- Run against staging first, then main.

ALTER TABLE idtype
    ADD COLUMN IF NOT EXISTS public BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE idtype
SET public = dataset.public
FROM dataset
WHERE idtype.mfid = dataset.unique_id
  AND idtype.resource_type = 'dataset';
