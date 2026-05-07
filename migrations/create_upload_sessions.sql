-- Tracks in-flight GCS resumable upload sessions.
-- Rows are deleted on successful completion (POST /datasets/{dsid}/upload/complete).
-- Expired rows (uploads abandoned by the client) can be purged with:
--   DELETE FROM upload_sessions WHERE expires_at < NOW();

CREATE TABLE IF NOT EXISTS upload_sessions (
    id                  SERIAL PRIMARY KEY,
    upload_id           TEXT NOT NULL UNIQUE,
    dataset_mfid        TEXT NOT NULL,
    gcs_path            TEXT NOT NULL,
    associated_file_name TEXT NOT NULL,
    owner_user_id       TEXT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS upload_sessions_expires_at_idx ON upload_sessions (expires_at);
