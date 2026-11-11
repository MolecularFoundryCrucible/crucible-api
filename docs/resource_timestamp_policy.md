# Timestamps

This page describes the `creation_time` and `modification_time` of projects, samples, datasets and instruments.

## Lifecycle times

Both fields are PostgreSQL `TIMESTAMPTZ` columns of the resource table (`idtype`), set by the database.

- Create and update requests do not accept either field.
- A database trigger sets both on creation, rejects changes to `creation_time`, and advances `modification_time` on every update of the resource row.
- The API updates that row on edits to the record and on changes to its files, thumbnails, keywords, links, access grants, visibility, owner, project, instrument, status or members, and when a deletion request is created or rejected.
- Responses serialize values in UTC with a `Z` suffix. Both fields are nullable; older records may have null values.

## Dataset timestamp

`dataset.timestamp` is producer-supplied scientific metadata: unconstrained text in any format or timezone, never normalized or used for the lifecycle times.
