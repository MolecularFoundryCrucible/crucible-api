-- Adds production GCS path to AssociatedFile.
-- NULL until the ingestion worker moves the file to mf-storage-prod and sets this field.
-- Format: "mf-storage-prod/{dsid}/{filename}"

ALTER TABLE associatedfile ADD COLUMN IF NOT EXISTS storage_path TEXT;
