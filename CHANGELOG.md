# Changelog

This changelog records changes that affect API users, operators, or downstream clients. Add entries under `Unreleased` in the same change that modifies the observable contract or operational behavior. Internal-only changes do not require an entry.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added `GET /instruments/{instrument_mfid}/service_accounts` for authorized administrators to inspect instrument bindings.
- Added a server warning when an unbound service account successfully creates a dataset for a registered instrument. Enforcement remains deferred.
- Added optional caller-specific `capabilities` to project and instrument responses, including the highest non-owner role the caller may grant. Capabilities are populated for singleton, exact-slug, create, update, and instrument-status responses, and remain null on general collection and search results.
- Added an explicit optional `instrument_mfid` filter to `GET /datasets` for stable instrument-scoped dataset listing.
- Added optional relationship filters: `sample_mfid` on `GET /datasets` and `dataset_mfid` on `GET /samples`. The existing nested relationship routes remain compatible but are deprecated.
- Added caller-specific capabilities to dataset and sample detail responses. Generic resource detail responses now populate capabilities consistently for datasets, samples, projects, and instruments.
- Added nullable `instrument` and `project` identity references to dataset response models. Existing flat `instrument_id`, `instrument_name`, and `project_id` fields remain available for compatibility.

### Changed

- Project membership now follows a strict hierarchy: editors manage contributor and viewer roles, admins manage editor and below, and owners manage admin and below. Project capabilities report the corresponding lower `max_grant_role`.
- Canonical and generic resource detail reads now share service loaders. `GET /resources/{resource_id}` mirrors canonical authorization semantics and avoids unnecessary deletion-request work for projects and instruments.
- Instrument display names now preserve caller-supplied casing instead of being forced to lowercase.
- Instrument collection filters and dataset assignment by `instrument_id` now resolve case-insensitively.
- Instrument search accepts an optional `status` filter with `active`, `maintenance`, and `decommissioned` values. Omitting it preserves the existing all-status search behavior.
- Project membership mutation routes now accept named roles (`viewer`, `contributor`, `editor`, `admin`, and `owner`) instead of integer enum values. `owner` remains rejected with 409 and requires the ownership-transfer route.
- Service accounts may no longer register instruments, including when they hold platform-admin status. Human platform administrators may continue registering an instrument for another identity.

### Fixed

- Project results from `GET /resources/metadata/search` now resolve titles through the canonical project MFID instead of the legacy project slug.
- Instrument updates now reject explicit null values for required identifiers, names, and locations with 422 instead of failing during persistence.
- Repeating a project add-member request with the member's current role remains idempotent, while requesting a different role now returns 409 and directs the caller to the role-update route instead of silently ignoring the requested change.
