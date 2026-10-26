import os
import re
from datetime import datetime, timezone
import hashlib

def remove_file(filepath):
    os.remove(filepath)

def is_orcid(val):
    pattern= re.compile("[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$")
    if re.match(pattern, val):
        return True
    else:
        return False

_MFID_PATTERN = re.compile(r"^[0-9a-hjkmnp-tv-z]{26}$")

def is_valid_mfid(val):
    """Check that val matches the mfid format: 26-character lowercase
    Crockford's Base32 (excludes i, l, o, u to avoid visual ambiguity)."""
    return bool(_MFID_PATTERN.match(val))


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


def checkhash(file):
    """Calculate SHA256 hash of a file.
    
    Args:
        file (str): Path to the file to hash
        
    Returns:
        str: Hexadecimal SHA256 hash of the file
    """
    with open(file,"rb") as f:
        fdata = f.read() 
        readable_hash = hashlib.sha256(fdata).hexdigest()
    return(readable_hash)


def if_match_satisfied(if_match, current_etag):
    if if_match is None or if_match.strip() == "*":
        return True
    current = current_etag.removeprefix("W/")
    return any(candidate.strip().removeprefix("W/") == current for candidate in if_match.split(","))
