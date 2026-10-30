# Resource timestamp policy

`idtype.creation_time` and `idtype.modification_time` are API-owned nullable PostgreSQL `TIMESTAMPTZ` lifecycle fields.

## Write behavior

- Public create and update models do not accept either lifecycle field.
- The database generates both values when a resource is created.
- Creation time is immutable after insertion.
- The database advances modification time whenever the resource row is updated.
- API responses serialize non-null values in canonical UTC `Z` form.

Legacy rows may retain null lifecycle values. The completed migration preserved unknown creation times as null and used creation time for a missing modification time only when creation time was known.

## Dataset timestamp

`dataset.timestamp` is producer-supplied scientific metadata. It remains unconstrained text, may use a domain-specific format or timezone, and is not normalized or used to repair lifecycle fields.

## History

The production conversion and enforcement migration completed on 2026-09-15 at Alembic revision `3f2b6a9c1d84`. The migration record is in git history.
