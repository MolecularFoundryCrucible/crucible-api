# Repository Agent Instructions

## Git workflow

- Changes must reach `main` through a pull request from `staging` or another reviewed branch.
- Never push directly to `main`, even when branch protection permits it.
- Never force-push `main`.
- Pushing to feature branches or `staging` is allowed when the user authorizes the release workflow.

## Change history

- Add an entry under `Unreleased` in `CHANGELOG.md` for changes that affect API users, operators, or downstream clients.
- Internal-only changes do not require a changelog entry.
