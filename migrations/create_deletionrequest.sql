-- Create the deletionrequest table for the soft-deletion workflow.
-- Run this in Google Cloud SQL Studio against the target database.

CREATE TABLE IF NOT EXISTS deletionrequest (
    id             SERIAL PRIMARY KEY,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    resource_name  TEXT,
    requester_id   TEXT NOT NULL,
    reason         TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    request_time   TEXT,
    review_time    TEXT,
    reviewer_id    TEXT,
    reviewer_notes TEXT
);

CREATE INDEX IF NOT EXISTS ix_deletionrequest_resource_type ON deletionrequest (resource_type);
CREATE INDEX IF NOT EXISTS ix_deletionrequest_resource_id   ON deletionrequest (resource_id);
CREATE INDEX IF NOT EXISTS ix_deletionrequest_requester_id  ON deletionrequest (requester_id);
CREATE INDEX IF NOT EXISTS ix_deletionrequest_status        ON deletionrequest (status);

-- If the table already exists, add the resource_name column:
ALTER TABLE deletionrequest ADD COLUMN IF NOT EXISTS resource_name TEXT;
