-- Enable trigram extension and add GIN indexes for fuzzy search endpoints.
-- Safe to run multiple times (IF NOT EXISTS).
-- Requires PostgreSQL superuser or pg_extension_owner membership.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_dataset_name_trgm   ON dataset USING gin(dataset_name   gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sample_name_trgm    ON sample  USING gin(sample_name    gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_project_title_trgm  ON project USING gin(title          gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_project_id_trgm     ON project USING gin(project_id     gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_user_username_trgm  ON "user"  USING gin(username       gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_user_firstname_trgm ON "user"  USING gin(first_name     gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_user_lastname_trgm  ON "user"  USING gin(last_name      gin_trgm_ops);
