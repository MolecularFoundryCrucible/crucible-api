-- Create the accessgroupjoinrequest table for the group join-request workflow.
-- Run this in Google Cloud SQL Studio against the target database.

CREATE TABLE IF NOT EXISTS accessgroupjoinrequest (
    id             SERIAL PRIMARY KEY,
    group_name     TEXT NOT NULL REFERENCES accessgroup(group_name),
    requester_id   TEXT NOT NULL REFERENCES "user"(orcid),
    reason         TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    request_time   TEXT,
    review_time    TEXT,
    reviewer_id    TEXT REFERENCES "user"(orcid),
    reviewer_notes TEXT
);

CREATE INDEX IF NOT EXISTS ix_agjr_group_name    ON accessgroupjoinrequest (group_name);
CREATE INDEX IF NOT EXISTS ix_agjr_requester_id  ON accessgroupjoinrequest (requester_id);
CREATE INDEX IF NOT EXISTS ix_agjr_status        ON accessgroupjoinrequest (status);
