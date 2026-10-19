import os
import json
import re
from datetime import datetime, timezone
from app.utils.constants import secret_store
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
    
def get_secret(secret_env_var, gcs_secret_name = None, sa_creds: str = None, cred_file: bool = False): 
    '''
    sa_creds defaults to None in which case it is assumed that you are running the function from a GCP instance with a service account that can authenticate itself. 
    If a value is provided, the function will check to see if the argument cred_file is set to True or False (default).

    If cred_file is True, then the sa_creds should be a filepath to the service account credentials.  
    If cred_file is False, then sa_creds should be the name of an environment variable in which the credentials are stored. 

    '''
    from dotenv import load_dotenv
    load_dotenv()
    secret = os.getenv(secret_env_var)
    if secret is None:
        if gcs_secret_name is None:
            raise RuntimeError(f"Secret {secret_env_var} is not set and has no Secret Manager name")
        secret = get_gcs_secret(f"{secret_store}/{gcs_secret_name}", sa_creds, cred_file)
        os.environ[secret_env_var] = secret
    return(secret)


def get_gcs_secret(secret_name, sa_creds, cred_file):
    from google.cloud import secretmanager
    from google.oauth2 import service_account
    import os
    home = os.getenv("HOME")
    
    if 'root' in home:
        # Assume you are running from a GCP service with SA that can auth itself 
        client = secretmanager.SecretManagerServiceClient() 

    elif cred_file is False:
        # use the credentials to authorize yourself!!
        J = json.loads(os.getenv("GCS_SA"))
        with open("temp_creds.json", "w") as f:
            json.dump(J, f)
        credentials = service_account.Credentials.from_service_account_file("temp_creds.json")
        client = secretmanager.SecretManagerServiceClient(credentials=credentials)

    else:
        # use the credentials file to authorize yourself!!
        credentials = service_account.Credentials.from_service_account_file(sa_creds)
        client = secretmanager.SecretManagerServiceClient(credentials=credentials)
        
    # get your secret
    response = client.access_secret_version(name=secret_name)
    secret_value = response.payload.data.decode("UTF-8")
    return(secret_value)


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
