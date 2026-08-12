ALTER TABLE deletionauditlog ADD COLUMN IF NOT EXISTS project_id VARCHAR;
CREATE INDEX IF NOT EXISTS ix_deletionauditlog_project_id ON deletionauditlog (project_id);
