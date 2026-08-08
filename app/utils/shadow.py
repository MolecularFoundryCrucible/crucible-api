"""Shadow-mode validation for the permission migration (phase 3).

Runs the new permission check alongside the old one, acts on the old answer,
logs where they differ. Safe because the new result is discarded, every call is
wrapped in its own try/except, and the whole thing is behind SHADOW_MODE.

The principal arrives via ContextVar because the pre-role helpers take `ag_ids`,
not a Principal; threading one through would mean editing ~37 call sites during
a phase meant to change nothing. Removed at cutover.

Costs one extra indexed query per check — contrary to the plan, the old check is
an EXISTS and leaves no rows in hand. Hence the flag.
"""
import logging
import os
import threading
from contextvars import ContextVar
from typing import Optional

from app.utils.permissions import Permission, Principal, effective_role, has_permission

logger = logging.getLogger(__name__)

SHADOW_MODE = os.environ.get("PERMISSION_SHADOW_MODE", "").lower() in ("1", "true", "yes")

# Match counts are the cutover gate: "no mismatches" is indistinguishable from
# "never exercised" without them.
SUMMARY_EVERY = int(os.environ.get("PERMISSION_SHADOW_SUMMARY_EVERY", "500"))

if SHADOW_MODE:
    # Otherwise "enabled and agreeing" looks exactly like "never switched on".
    logger.warning(
        "permission_shadow_enabled summary_every=%d — new checks run alongside "
        "the old ones; the old answer is authoritative", SUMMARY_EVERY,
    )

current_principal: ContextVar[Optional[Principal]] = ContextVar(
    "current_principal", default=None
)

_lock = threading.Lock()
_counts: dict[tuple[str, str], dict[str, int]] = {}
_total = 0


def _bump(check: str, outcome: str, permission: str) -> None:
    global _total
    with _lock:
        bucket = _counts.setdefault((check, permission), {})
        bucket[outcome] = bucket.get(outcome, 0) + 1
        _total += 1
        # Also on the very first check, so a low-traffic environment gets
        # confirmation it is running without waiting for 500 of them.
        due = _total == 1 or _total % SUMMARY_EVERY == 0
        snapshot = dict(_counts) if due else None
    if snapshot:
        _log_summary(snapshot)


def _log_summary(snapshot) -> None:
    parts = []
    for (check, permission), bucket in sorted(snapshot.items()):
        parts.append(
            f"{check}/{permission}"
            f" match={bucket.get('match', 0)}"
            f" mismatch={bucket.get('mismatch', 0)}"
            f" error={bucket.get('error', 0)}"
        )
    logger.warning("permission_shadow_summary %s", " | ".join(parts))


def report() -> dict:
    """Current counts, keyed "check/permission"."""
    with _lock:
        return {f"{c}/{p}": dict(b) for (c, p), b in _counts.items()}


def reset() -> None:
    global _total
    with _lock:
        _counts.clear()
        _total = 0


def _fetch_acl(session, mfid):
    from sqlalchemy import text
    rows = session.execute(
        text("SELECT accessgroup_id, role FROM resourceacl WHERE mfid = :m"),
        {"m": mfid},
    ).all()
    return [(gid, role) for gid, role in rows if role is not None]


def compare(session, check: str, mfid: str, permission: Permission,
            allowed_old: bool) -> None:
    """Compare the new answer against `allowed_old` and log any difference.

    Never raises, never influences the caller. Output is a log line and a counter.
    """
    if not SHADOW_MODE:
        return
    try:
        principal = current_principal.get()
        if principal is None:
            return  # background task or non-request caller

        acl_rows = _fetch_acl(session, mfid)
        allowed_new = has_permission(principal, acl_rows, permission)

        if allowed_new == allowed_old:
            _bump(check, "match", permission.value)
            return

        _bump(check, "mismatch", permission.value)
        role = effective_role(principal, acl_rows)
        logger.warning(
            "permission_shadow_mismatch check=%s permission=%s mfid=%s user=%s "
            "sa=%s platform_role=%s old=%s new=%s effective_role=%s groups=%d acl_rows=%d",
            check, permission.value, mfid, principal.user_id,
            principal.is_service_account, principal.platform_role.value,
            allowed_old, allowed_new,
            role.name.lower() if role else None,
            len(principal.groups), len(acl_rows),
        )
    except Exception:
        _bump(check, "error", permission.value)
        logger.exception(
            "permission_shadow_error check=%s permission=%s mfid=%s", check, permission, mfid
        )
