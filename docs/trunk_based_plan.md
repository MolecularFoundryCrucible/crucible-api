# Trunk-based crucible-api: switch plan

Status: direction approved by the user 2026-10-30; this plan awaits the user's review. Staging is already deployed by mf-crucible-deploy (`foundry/staging`); production still deploys from this repository's `crucible-api-main` trigger.

## Target

- One long-lived branch, `main`. Every change is a pull request to `main`, merged after its checks pass.
- Every merge to `main` builds one image, `us-central1-docker.pkg.dev/mf-crucible/crucible-api/crucible-api`, tagged with the full commit hash and a moving `main`. The deploy repository updates `foundry/staging` to its digest and deploys it.
- A release is a version-bump pull request merged to `main`, then a `vX.Y.Z` tag on that commit. The tag build copies the commit's image to `crucible-api-releases/crucible-api:X.Y.Z` (same digest, no rebuild). Production changes only by an approved pull request in mf-crucible-deploy that pins that digest (approvers: the user, Ed Barnard, Tim Kodalle; any one).
- This repository holds no deployment configuration: `k8s-conf/`, the kubectl steps, `scripts/deploy_hub_auth_cleanup.sh`, and `tests/test_k8s_pinned_settings.py` are removed after production's cut-over.

## Guarding `main`

GitHub branch protection and rulesets are not available on this private repository's plan (the API answers "Upgrade to GitHub Pro or make this repository public"). Until the plan changes or the repository becomes public (Phase 4), `main` is guarded by convention and by the deploy design: merging to `main` reaches staging only; production needs a release tag and an approved deploy pull request. Pull requests only, no direct pushes or force-pushes; the user merges. If the organization can upgrade, require the four CI checks and one review on `main`.

## Steps

1. Interim, now: #397 makes staging builds image-only, so `foundry/staging` has one owner.
2. `cloudbuild.yaml`: on `main`, push `crucible-api/crucible-api:<sha>` and `:main` as well as today's `crucible-api-main:<short sha>` (production's trigger still deploys the latter until step 6). Staging's updater in mf-crucible-deploy keeps following `crucible-api-staging` until step 4.
3. `cloudbuild.release.yaml`: copy `crucible-api/crucible-api:<tag commit sha>` to the release repository instead of building (fail if that image does not exist); check the copied image's labels and version as today.
4. Retire `staging`: merge `staging` into `main` (they are equal after 3.3.0 apart from #397), point open pull requests at `main`, update `AGENTS.md` ("Changes reach `main` through a reviewed pull request; production deploys only from release tags through mf-crucible-deploy"), tell the nano-crucible, crucible-hub, and crucible-tests sessions, and archive the branch (rename to `archive/staging`, then delete after a month). The deploy updater flips to `crucible-api/crucible-api` with this step.
5. After step 4 (so staging never loses its image source), delete the `crucible-api-staging` trigger, and `crucible-api-deprecated-versions` (matches `v1` to `v9` branches; none exist).
6. Production cut-over, with one deployer at every moment: (a) remove the kubectl step from `cloudbuild.yaml` (from then on a merge to `main` deploys nothing to production); (b) mf-crucible-deploy deploys `foundry/main`, already pinned to the 3.3.0 release digest, through its approved trigger and verifies it. No merges to `main` between (a) and (b) need production. Then `crucible-api-main:*` images stop being built.
7. Remove `k8s-conf/`, `scripts/deploy_hub_auth_cleanup.sh`, `tests/test_k8s_pinned_settings.py`, and the Foundry manifest cases in `tests/test_settings_check.py` (the deploy repository runs `settings check` on its manifests).

## Rollback

Until step 6, production deploys exactly as today. Staging rolls back by reverting `foundry/staging`. A release rolls back by reverting the `foundry/main` pin.
