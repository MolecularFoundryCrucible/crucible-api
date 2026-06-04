-- Adds optional username to the user table.
-- Format: 3-32 chars, ^[a-z][a-z0-9_-]{2,31}$, unique (NULLs are exempt).

ALTER TABLE "user" ADD COLUMN IF NOT EXISTS username VARCHAR(32);

CREATE UNIQUE INDEX IF NOT EXISTS ix_user_username ON "user" (username)
    WHERE username IS NOT NULL;
