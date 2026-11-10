# Secret key separation: follow-up

Status: proposed 2026-10-26, not scheduled. Security-sensitive; changes signatures on deploy.

## Today

`CRUCIBLE_SECRET_KEY` does three jobs:

1. Until 4.0.0 it signed the ORCID browser session cookie of the page that handed out an API key. That page and its cookie were removed in 4.0.0, so this job is gone.
2. Signs pagination cursors when neither `CRUCIBLE_CURSOR_SIGNING_KEY` nor `CRUCIBLE_CURSOR_SIGNING_KEYS` is set (`_signing_keys` in `app/utils/cursor_utils.py`), which is the case on staging and production.
3. Computes the principal and filter fingerprints inside every cursor (`_digest` in `app/utils/cursor_utils.py`), always with this key, even when separate cursor keys are configured.

Hub tokens are stored as plain SHA-256 hashes and do not use it.

There is no practical cross-use attack: the three uses sign different, non-overlapping formats. The costs are operational:

- Rotating the key logs out every browser session and breaks every outstanding cursor at once. The cursor key ring does not isolate cursors from it, because the fingerprints ignore the ring.
- A leaked key allows forging session cookies and cursors together.

## Practice

- NIST SP 800-57 Part 1 Rev. 5, as quoted by the OWASP Key Management Cheat Sheet: "In general, a single key should be used for only one purpose", because reuse may weaken one or both uses, limiting use limits the damage of a compromise, and different uses interfere (for example how long each must be kept).
- RFC 5869 (HKDF) and NIST SP 800-108r1 standardize deriving several independent keys from one secret, each bound to a purpose label (`info`, or label and context).
- Web frameworks keep one operator-facing secret and derive a key per purpose: Django (`salted_hmac`, signing salts as namespaces), Rails (`ActiveSupport::KeyGenerator`), itsdangerous and Flask (salts).

## Proposal

1. Keep `CRUCIBLE_SECRET_KEY` as the one value operators set.
2. Derive a key per purpose with `HKDFExpand` from the `cryptography` package (the secret is already high-entropy), with versioned labels such as `crucible session cookie v1` and `crucible cursor v1`. Declare `cryptography` in `pyproject.toml`; today it arrives only through `joserfc` and `authlib`.
3. Use the derived cursor key as the default cursor key, and compute the cursor fingerprints with the cursor key ring, so `CRUCIBLE_CURSOR_SIGNING_KEYS` rotates everything a cursor carries.
4. Keep `CRUCIBLE_CURSOR_SIGNING_KEY(S)` as optional overrides.

On deploy, every legacy browser session is logged out once and outstanding cursors stop working once (clients restart from the first page). Announce it with the release.

## Sources

- OWASP Key Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html
- NIST SP 800-57 Part 1 Rev. 5: https://doi.org/10.6028/NIST.SP.800-57pt1r5
- NIST SP 800-108 Rev. 1: https://csrc.nist.gov/pubs/sp/800/108/r1/final
- RFC 5869: https://www.rfc-editor.org/rfc/rfc5869
- Django signing: https://docs.djangoproject.com/en/stable/topics/signing/
- Rails KeyGenerator: https://api.rubyonrails.org/classes/ActiveSupport/KeyGenerator.html
- itsdangerous: https://itsdangerous.palletsprojects.com/en/stable/concepts/
- cryptography HKDF: https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/
