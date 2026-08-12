-- Permanent audit log for hard-deleted resources.
-- Written before any rows are destroyed, so the record survives the deletion.
-- Run this in Google Cloud SQL Studio against the target database.

CREATE TABLE IF NOT EXISTS deletionauditlog (
    id             SERIAL PRIMARY KEY,
    resource_id    TEXT NOT NULL,
    resource_type  TEXT,
    resource_name  TEXT,
    requester_id   TEXT NOT NULL,
    reason         TEXT,
    request_time   TEXT,
    reviewer_id    TEXT NOT NULL,
    reviewer_notes TEXT,
    review_time    TEXT,
    deleted_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_deletionauditlog_resource_id  ON deletionauditlog (resource_id);
CREATE INDEX IF NOT EXISTS ix_deletionauditlog_requester_id ON deletionauditlog (requester_id);
CREATE INDEX IF NOT EXISTS ix_deletionauditlog_reviewer_id  ON deletionauditlog (reviewer_id);
CREATE INDEX IF NOT EXISTS ix_deletionauditlog_deleted_at   ON deletionauditlog (deleted_at);
