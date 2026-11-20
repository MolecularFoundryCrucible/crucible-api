---
name: release
description: Prepare a Crucible API release - version, changelog, upgrade notes and the release pull request. Use only when the user asks to release, cut a version or prepare a release pull request. The user creates the tag.
---

# Prepare a release

The tag is the user's. Your part ends with a merged release pull request and a list of what to do after tagging.

## 1. Before

- `main` holds everything the release should contain, and staging has run that commit.
- For a major version, the Hub, nano-crucible and the deploy repository are ready to ship with it; ask.

## 2. The release pull request

1. Set `version` in `pyproject.toml` and run `uv lock`.
2. In `CHANGELOG.md`, turn `Unreleased` into `## [X.Y.Z] - YYYY-MM-DD` and start an empty `Unreleased`. One short line per change, breaking changes first, entries that a later one superseded merged into it.
3. For a release with operator steps, write or update `docs/upgrading/<from>-to-<to>.md`: what to set before, the cut-over in order, the cleanup after.
4. Run every check (skill `run-checks`), including the `selfhost` job.
5. Open the pull request. Its body is a `## Summary` of one-line bullets.

## 3. After the user merges and tags `vX.Y.Z`

- The tag must match `version` in `pyproject.toml`, or the release build refuses it.
- The release build copies the image already built for that commit on `main`; it does not rebuild.
- The tag also publishes `ghcr.io/molecularfoundrycrucible/crucible-api:X.Y.Z` for self-hosting (`release-image.yml`). crucible-selfhost pins it in its own pull request; tell the user.
- Publishing the GitHub release attaches the OpenAPI document (`crucible-api.json`) that clients pin.
- Production changes only through the deploy repository, following the upgrade guide.

## Pre-releases

A pre-release such as `4.0.0a1` is a version in `pyproject.toml` on `main`, not a tag: the release build and the deployment gates accept only `vX.Y.Z`. Staging runs every `main` build, so the pre-release is tested there, and its image is the `main` build of that commit. The changelog section carries the pre-release version until the final release renames it.

## Never

Tag, push to `main`, or deploy.
