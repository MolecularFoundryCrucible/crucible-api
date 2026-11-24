import re
from datetime import datetime, timezone

def refuse_public_change(data):
    """For update models: whether a record is public changes only through its access routes, so a
    request trying to change it here fails instead of quietly doing nothing."""
    if isinstance(data, dict) and "public" in data:
        raise ValueError("public cannot be changed here; use PUT or DELETE /resources/{mfid}/access/public, or upgrade nano-crucible")
    return data


def refuse_instrument_name(data):
    """instrument_name was removed as an input in 4.0.0. A null value from an older client is dropped;
    a name is refused, because display names are not identifiers."""
    if isinstance(data, dict) and "instrument_name" in data:
        if data["instrument_name"] not in (None, ""):
            raise ValueError("instrument_name is not accepted; name the instrument with instrument_id or instrument_mfid")
        data = {key: value for key, value in data.items() if key != "instrument_name"}
    return data


def fold_owner_ref(model):
    """Create models accept owner_ref and the deprecated owner and owner_orcid; at most one may be given.
    owner_ref is carried in owner, which the routes resolve."""
    values = {name: getattr(model, name, None) for name in ("owner_ref", "owner", "owner_orcid")}
    given = [name for name, value in values.items() if value is not None]
    if len(given) > 1 and len({values[name] for name in given}) > 1:
        raise ValueError(f"Provide only owner_ref, not {' and '.join(given)}")
    if model.owner_ref is not None:
        model.owner = model.owner_ref
    return model


def is_orcid(val):
    return re.fullmatch("[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}", val) is not None

_MFID_PATTERN = re.compile(r"[0-9a-hjkmnp-tv-z]{26}")

def is_valid_mfid(val):
    """Check that val matches the mfid format: 26-character lowercase
    Crockford's Base32 (excludes i, l, o, u to avoid visual ambiguity)."""
    return _MFID_PATTERN.fullmatch(val) is not None


def blank_to_none(val):
    """Normalise a blank string to None for optional identifier fields.

    "" is a real value: it matches rows, satisfies foreign keys, and creates
    access groups. Uploaders sending project_id="" built a nameless project
    holding 1,292 datasets. Coerced rather than rejected because absent is
    legitimate here; fields where the value IS the identity must reject.
    """
    if isinstance(val, str) and not val.strip():
        return None
    return val


def get_utc_isoformat():
    return datetime.now(timezone.utc).isoformat()


def get_utc_datetime():
    return datetime.now(timezone.utc)


def if_match_satisfied(if_match, current_etag):
    if if_match is None or if_match.strip() == "*":
        return True
    current = current_etag.removeprefix("W/")
    return any(candidate.strip().removeprefix("W/") == current for candidate in if_match.split(","))


def merge_patch(target, patch):
    """Apply a JSON Merge Patch (RFC 7396): null removes a key, objects merge, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result
