# Changelog

This changelog records changes that affect API users, operators, or downstream clients. Add entries under `Unreleased` in the same change that modifies the observable contract or operational behavior. Internal-only changes do not require an entry.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Dataset creation now accepts either `instrument_id` or `instrument_mfid`, or a matching pair. Existing `instrument_id` and legacy `instrument_name` requests remain supported.
- Added `project_id` or `project_mfid` and optional `project_scope=assigned|shared|all` filters to dataset and sample search. Existing `project_id` searches remain assigned-only by default, and filtered results include `project_relation` metadata.
- Added explicit `project_id` or `project_mfid` and optional `project_scope=assigned|shared|all` filters to dataset and sample collections. Matching slug and MFID pairs are accepted, conflicting pairs return 422, and filtered responses include additive `project_relation` metadata.
- Added fixed `image/jpeg` MIME metadata to thumbnail responses and documented the bulk first-thumbnail response schema in OpenAPI.
- Added `PATCH /datasets/{dataset_mfid}/thumbnails/{thumbnail_id}` for authorized thumbnail renaming and image replacement while preserving thumbnail identity and ordering.
- Added thumbnail IDs to bulk first-thumbnail responses so clients can target modification or deletion routes.
- Added a concurrent database index supporting dataset-scoped thumbnail listing, selection, and deletion.
- Added `GET /instruments/{instrument_mfid}/service_accounts` for authorized administrators to inspect instrument bindings.
- Added a server warning when an unbound service account successfully creates a dataset for a registered instrument. Enforcement remains deferred.
- Added optional caller-specific `capabilities` to project and instrument responses, including the highest non-owner role the caller may grant. Capabilities are populated for singleton, exact-slug, create, update, and instrument-status responses, and remain null on general collection and search results.
- Added an explicit optional `instrument_mfid` filter to `GET /datasets` for stable instrument-scoped dataset listing.
- Added optional relationship filters: `sample_mfid` on `GET /datasets` and `dataset_mfid` on `GET /samples`. The existing nested relationship routes remain compatible but are deprecated.
- Added caller-specific capabilities to dataset and sample detail responses. Generic resource detail responses now populate capabilities consistently for datasets, samples, projects, and instruments.
- Added nullable `instrument` and `project` identity references to dataset response models. Existing flat `instrument_id`, `instrument_name`, and `project_id` fields remain available for compatibility.
- Added a nullable `project` identity reference to sample response models. The existing flat `project_id` field remains available for compatibility.
- Added `include_datasets=false` to singleton sample and generic resource reads so clients can omit the legacy embedded dataset collection. `include_links` and `GET /datasets?sample_mfid=...` are the preferred relationship interfaces.

### Changed

- Trigram searches now normalize bounded input, treat prefix wildcard characters literally, bind the request-local similarity threshold, and use canonical unique IDs as deterministic relevance tie-breakers.
- Dataset, sample, instrument, and generic resource singleton GETs now resolve the public-safe owner object by default. Pass `include_owner=false` to omit it. Collection and search expansion remains opt-in.
- Large bulk first-thumbnail lookups now emit a structured operator warning above a configurable threshold without rejecting the request, and duplicate identifiers are removed before querying.
- Thumbnail uploads now release their database connection during image processing and recheck edit authorization before writing.
- Concurrent thumbnail uploads to the same dataset now serialize their dataset-scoped idempotency check, preventing identical requests from creating duplicate rows.
- Deletion-request and join-request collections now resolve project information in their page queries instead of issuing per-item queries. File detail responses reuse the already authorized dataset instead of lazily loading it again.
- Project membership now follows a strict hierarchy: editors manage contributor and viewer roles, admins manage editor and below, and owners manage admin and below. Project capabilities report the corresponding lower `max_grant_role`.
- Canonical and generic resource detail reads now share service loaders. `GET /resources/{resource_id}` mirrors canonical authorization semantics and avoids unnecessary deletion-request work for projects and instruments.
- Instrument display names now preserve caller-supplied casing instead of being forced to lowercase.
- Instrument collection filters and dataset assignment by `instrument_id` now resolve case-insensitively.
- Instrument search accepts an optional `status` filter with `active`, `maintenance`, and `decommissioned` values. Omitting it preserves the existing all-status search behavior.
- Project membership mutation routes now accept named roles (`viewer`, `contributor`, `editor`, `admin`, and `owner`) instead of integer enum values. `owner` remains rejected with 409 and requires the ownership-transfer route.
- Service accounts may no longer register instruments, including when they hold platform-admin status. Human platform administrators may continue registering an instrument for another identity.

### Fixed

- Identical thumbnail images uploaded to different datasets now remain independently owned instead of moving an existing thumbnail between datasets.
- Thumbnail uploads now verify dataset edit access before decoding or processing image data.
- Thumbnail deletion now applies dataset edit authorization in its initial lookup, consistently returning 404 for absent or inaccessible datasets and avoiding a redundant permission query.
- Thumbnail uploads now strictly validate base64 input and return stable errors without exposing image-library exception details.
- Thumbnail normalization now enforces configurable 50 MiB and 50 megapixel source limits, applies EXIF orientation, uses the first animation frame, composites transparency onto white, and accepts common raster formats only.
- Dataset thumbnail lists are now ordered by thumbnail ID, matching the bulk endpoint's definition of the first thumbnail.
- Join-request review now locks the pending request during its state transition, and the database prevents concurrent duplicate pending requests for the same user and access group.
- Project results from `GET /resources/metadata/search` now resolve titles through the canonical project MFID instead of the legacy project slug.
- Instrument updates now reject explicit null values for required identifiers, names, and locations with 422 instead of failing during persistence.
- Repeating a project add-member request with the member's current role remains idempotent, while requesting a different role now returns 409 and directs the caller to the role-update route instead of silently ignoring the requested change.
