-- Adds storage_backend and access_note to AssociatedFile for cataloging files
-- that live outside GCS (HPC partitions, Globus-addressable paths, etc).
-- storage_backend = 'gcs' triggers signed-URL generation; any other value is
-- catalog-only. Existing rows default to 'gcs' (their current, only, meaning).

ALTER TABLE associatedfile ADD COLUMN IF NOT EXISTS storage_backend TEXT NOT NULL DEFAULT 'gcs';
ALTER TABLE associatedfile ADD COLUMN IF NOT EXISTS access_note TEXT;
