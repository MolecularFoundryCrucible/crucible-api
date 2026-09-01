# Changelog

This changelog records changes that affect API users, operators, or downstream clients. Add entries under `Unreleased` in the same change that modifies the observable contract or operational behavior. Internal-only changes do not require an entry.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added `GET /instruments/{instrument_mfid}/service_accounts` for authorized administrators to inspect instrument bindings.
- Added a server warning when an unbound service account successfully creates a dataset for a registered instrument. Enforcement remains deferred.

### Changed

- Instrument display names now preserve caller-supplied casing instead of being forced to lowercase.
- Instrument collection filters and dataset assignment by `instrument_id` now resolve case-insensitively.
- Project membership mutation routes now accept named roles (`viewer`, `contributor`, `editor`, `admin`, and `owner`) instead of integer enum values. `owner` remains rejected with 409 and requires the ownership-transfer route.

### Fixed

- Instrument updates now reject explicit null values for required identifiers, names, and locations with 422 instead of failing during persistence.
