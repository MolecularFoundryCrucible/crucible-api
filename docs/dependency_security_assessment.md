# Dependency security assessment

Date: 2026-09-28. Assessed `feat/hub-login-design` at `b35c825`, using `pyproject.toml`, `uv.lock`, application call paths, and current GitHub advisory records. Assessment only: no dependency, runtime, database, or deployment changes.

## Result

The lockfile contains affected versions, but the inspected call paths do not establish an exploitable Hub authentication bypass from these dependency alerts. This is not a clean security bill: indirect SDK paths, native decoder behavior, container packages, and deployed versions were not exhaustively audited. Existing ingress and credential-lifecycle findings remain separate release decisions.

Use small, separately tested upgrades. Advisory severity describes the vulnerable library configuration, not demonstrated severity in this API. Versions below are advisory fix floors, not tested upgrade selections or guarantees that all advisories are resolved.

## Findings and priorities

### DEP-01: Outbound HTTP dependencies

Priority: first maintenance batch. Upstream severity: high for several urllib3 advisories; API exploitability unconfirmed.

`uv.lock:2203` contains urllib3 2.1.0. Requests and Google SDKs use this dependency. Potential impacts include resource exhaustion from malicious compressed responses and credential forwarding under specific redirect configurations. [Streaming redirect advisory](https://github.com/urllib3/urllib3/security/advisories/GHSA-38jv-5279-wg99), [low-level proxy redirect advisory](https://github.com/urllib3/urllib3/security/advisories/GHSA-qccp-gfcp-xxvc).

The Hub ORCID exchange uses configured issuer endpoints, ten-second timeouts, and disabled redirects (`app/utils/hub_orcid.py:52`, `:61`). Legacy ORCID and proposal-service requests also use fixed/configured services, not arbitrary caller-supplied hosts. No direct application use of the affected low-level proxy redirect API was found. SDK internals were not exhaustively traced.

Recommendation: resolve and test a supported urllib3 2.x release covering the reviewed fixes, whose highest reported floor is 2.7.0. Check SDK constraints, ORCID exchange, signed downloads, and storage operations before selecting the exact version. Updating urllib3 does not itself bound all ordinary response sizes.

### DEP-02: Image decoding

Priority: separate near-term upgrade. Upstream severity: high, including native memory corruption; a matching exploit path was not established.

Pillow 11.1.0 is pinned at `uv.lock:1462`. Authenticated dataset editors can submit thumbnail bytes (`app/api/routes/datasets.py:1262`). The normalizer bounds encoded/raw size and pixel count, treats decompression warnings as errors, and passes an explicit format allowlist to `Image.open` before decoding (`app/services/thumbnails.py:10`, `:39`).

PSD, JPEG2000, FITS, PDF, and font formats involved in reviewed alerts are excluded. The coordinate-overflow advisory requires attacker-controlled operation coordinates; our module-level `Image.alpha_composite` call receives equally sized images and no user-provided coordinates. This mitigation assessment does not certify the allowed decoders against every malformed input. [Coordinate-overflow advisory](https://github.com/python-pillow/Pillow/security/advisories/GHSA-6r8x-57c9-28j4).

Recommendation: a dedicated Pillow upgrade with malformed-image, allowlist, EXIF, transparency, and size-limit tests. Reviewed advisories report fix floors through 12.3.0, crossing our current major version. Verify published release availability, Python support, and imageio compatibility before choosing a target.

### DEP-03: Web framework stack

Priority: coordinated upgrade, not an isolated Starlette override. Upstream severity: high for some DoS advisories; matching handlers not found.

FastAPI 0.115.14 requires Starlette `>=0.40.0,<0.47.0`; the lock has Starlette 0.46.2 (`uv.lock:2144`). Patching outside that range requires a compatible FastAPI upgrade. Reviewed Starlette fixes extend through 1.3.1 and python-multipart fixes through 0.0.31; the latter is locked at 0.0.20.

No application `FileResponse`, `StaticFiles`, `UploadFile`, `request.form()`, or `HTTPEndpoint` use was found. Those alerts must not be described as confirmed reachable upload/file-serving vulnerabilities. Hub auth path classification uses `request.scope["path"]`, not the Host-derived `request.url.path` (`app/api/main.py:50`). A URL-derived audit fallback remains at line 85, so malformed-host behavior still deserves regression coverage. [Range parsing](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8), [multipart rollover](https://github.com/Kludex/starlette/security/advisories/GHSA-2c2j-9gv5-cj73), [Host-derived path advisory](https://github.com/advisories/GHSA-86qp-5c8j-p5mr).

Recommendation: upgrade FastAPI, Starlette, and multipart together after resolving supported versions. Run the full suite, compare generated OpenAPI, and verify validation errors, pagination, middleware headers, redirects, and existing API-key clients. Do not add request-time security machinery merely to silence unused-feature alerts.

### DEP-04: Legacy JWT and lower-exposure dependencies

Priority: small independent maintenance batches. No matching application exploit path established.

- python-jose 3.3.0: upgrade to a supported release at least 3.4.0. The legacy decoder fixes HS256 and a symmetric secret (`app/utils/auth_utils.py:37`, `:154`); the [OpenSSH ECDSA confusion advisory](https://github.com/advisories/GHSA-6c5p-j8vq-pqhj) does not match that configuration. No JWE decoding was found. Preserve legacy JWT behavior in regression tests. New Hub login uses Authlib/joserfc, not python-jose.
- AnyIO 4.13.0: reviewed fix floor 4.14.2. The [TLS hostname advisory](https://github.com/advisories/GHSA-82r6-8w77-94w6) requires internationalized destination hostnames and connection redirection; the inspected ORCID destinations are fixed ASCII hosts. No application process-pool use was found for the other reviewed alert.
- pyasn1 0.6.3 and idna 3.13: reviewed fix floors 0.6.4 and 3.15 respectively. Malicious parsing/encoding inputs are the concern; no direct attacker-controlled application call was established. Test their authentication and HTTP consumers after resolution.
- pg8000 1.31.2: reviewed SQL-injection fix floor 1.31.5. The API engine uses SQLAlchemy's default `postgresql://` driver, not explicit pg8000 (`app/utils/sql_utils.py:25`); no pg8000 application imports were found. Confirm tooling consumers before upgrading or removing it.
- Tornado 6.5.5: reviewed fixes extend through 6.5.8. It arrives with runtime-listed notebook tooling; no Tornado application server/client use was found. Consider moving notebook tooling out of the production dependency group only after checking operator workflows.
- ecdsa 0.19.2: the reviewed signing timing advisory has no fixed version listed. No application ECDSA signing was found. Do not claim that updating python-jose necessarily removes this transitive alert.

## Implementation sequence

1. Upgrade urllib3 alone after resolving compatible constraints. Review the lockfile diff for unrelated changes.
2. Upgrade legacy JWT dependencies with old-token validation tests; do not replace the authentication scheme.
3. Upgrade Pillow with thumbnail regression tests.
4. Upgrade the framework stack with the full test suite and OpenAPI/client compatibility checks.
5. Update remaining transitive dependencies and assess unused runtime tooling separately.

Re-query advisories when each batch starts. Test the actual locked build; do not treat minimum fix versions as final targets. No migration or intentional API contract change is proposed. No tests or exploit payloads were run for this assessment, and the existing baseline test results do not validate future upgrades.
