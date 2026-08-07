"""Shadow-mode validation for the permission migration.

Phase 3 of docs/permission_system_plan.md. Runs the new permission check
alongside the old one on real traffic, acts on the **old** answer, and logs
where they differ.

Three properties keep it safe:

- the new result is discarded, so a bug in it cannot affect any request
- every call is wrapped in its own try/except, so unfinished code cannot take
  down a working route
- it is behind SHADOW_MODE, so it can be switched off without a deploy

The principal reaches this module through a ContextVar rather than a function
argument. The pre-role helpers take `ag_ids` (a list of ints), not a Principal,
and threading one through would mean editing ~37 call sites during a phase
whose whole point is changing nothing. The ContextVar is set by authorize_user
and disappears at cutover, when those signatures change properly anyway.

Note on cost: the plan claimed the ACL rows would already be in hand from the
old check. They are not — the old check is an EXISTS and returns no rows — so
this issues one extra small indexed query per check. That is acceptable behind
a flag and is the reason the flag exists.
"""
import logging
import os
import threading
from contextvars import ContextVar
from typing import Optional

from app.utils.permissions import Permission, Principal, effective_role, has_permission

logger = logging.getLogger(__name__)

SHADOW_MODE = os.environ.get("PERMISSION_SHADOW_MODE", "").lower() in ("1", "true", "yes")

# How often to emit the running per-check summary. Match counts are the exit
# criterion for the cutover — "no mismatches" is indistinguishable from "this
# route was never exercised" without them.
SUMMARY_EVERY = int(os.environ.get("PERMISSION_SHADOW_SUMMARY_EVERY", "500"))

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
        due = _total % SUMMARY_EVERY == 0
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
    """Current counts, for a diagnostic route or a test."""
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
    """Compute the new answer, compare, log. Never raises.

    `allowed_old` is what the caller is about to act on. Nothing here changes
    that — the only output is a log line and a counter.
    """
    if not SHADOW_MODE:
        return
    try:
        principal = current_principal.get()
        if principal is None:
            # No authenticated principal in this context (a background task, or
            # a helper called outside a request). Nothing to compare against.
            return

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
