---
name: release
description: Use when asked to release, cut a version or prepare a release pull request for the Crucible API - version, changelog, upgrade notes. Not for routine changes; the maintainers create the tag.
---

# Prepare a release

The tag belongs to the maintainers. This workflow ends with a merged release pull request and a list of what follows the tag.

## 1. Before

- `main` holds everything the release should contain, and staging has run that commit.
- For a major version, ask whether the Hub, nano-crucible, crucible-selfhost and the Foundry's deploy repository (mf-crucible-deploy) are ready to ship with it.

## 2. The release pull request

1. Set `version` in `pyproject.toml` and run `uv lock`.
2. In `CHANGELOG.md`, turn `Unreleased` into `## [X.Y.Z] - YYYY-MM-DD` and start an empty `Unreleased`. One short line per change, breaking changes first, entries that a later one superseded merged into it.
3. For a release with operator steps, write or update `docs/upgrading/<from>-to-<to>.md`: what to set before, the cut-over in order, the cleanup after.
4. Run the checks (skill `run-checks`), including the `selfhost` job.
5. Open the pull request. Its body is a `## Summary` of one-line bullets.

## 3. After the tag `vX.Y.Z`

| What | Done by |
|---|---|
| Refuses a tag that differs from `version` in `pyproject.toml` | GitHub Actions (`release-image.yml`) |
| Adds `X.Y.Z` and `X.Y` to `ghcr.io/molecularfoundrycrucible/crucible-api:sha-<commit>`, the amd64 and arm64 image `main` built and staging ran, without rebuilding; fails when that image does not exist | GitHub Actions (`release-image.yml`) |
| Attaches the OpenAPI document (`crucible-api.json`) when the GitHub release is published | GitHub Actions (`openapi.yml`) |
| Pins the new image | A pull request in crucible-selfhost |
| Opens the production pull request pinning `X.Y.Z`, and deploys it following the upgrade guide | mf-crucible-deploy |

## Pre-releases

A pre-release such as `4.0.0a1` is a version in `pyproject.toml` on `main`, not a tag: the release builds accept only `vX.Y.Z`. Staging runs every `main` build, so the pre-release is tested there, and its image is the `main` build of that commit. The changelog section carries the pre-release version until the final release renames it.

## Never

Tag, push to `main`, or deploy.
