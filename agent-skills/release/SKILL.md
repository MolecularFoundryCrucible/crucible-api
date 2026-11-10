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
4. Update the Hub release pinned in `deploy/compose/README.md` and `.github/workflows/tests.yml` if a newer one is required.
5. Run every check (skill `run-checks`), including both bundle jobs.
6. Open the pull request. Its body is a `## Summary` of one-line bullets.

## 3. After the user merges and tags `vX.Y.Z`

- The tag must match `version` in `pyproject.toml`, or the release build refuses it.
- The release build copies the image already built for that commit on `main`; it does not rebuild.
- Publishing the GitHub release attaches the OpenAPI document (`crucible-api.json`) that clients pin.
- Production changes only through the deploy repository, following the upgrade guide.

## Never

Tag, push to `main`, or deploy.
