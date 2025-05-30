import os
import subprocess as sp
import json
import pytz
from datetime import datetime
from app.utils.constants import secret_store


def remove_file(filepath):
    os.remove(filepath)

def run_shell(cmd, checkflag = True, background = False):
    if background:
        return(sp.Popen(cmd, stdout = sp.PIPE, stderr = sp.STDOUT, shell = True, universal_newlines = True))
    return(sp.run(cmd, stdout = sp.PIPE, stderr = sp.STDOUT, shell = True, universal_newlines = True, check = checkflag))



def get_secret(secret_env_var, gcs_secret_name = None, sa_creds: str = None, cred_file: bool = False): 
    '''
    sa_creds defaults to None in which case it is assumed that you are running the function from a GCP instance with a service account that can authenticate itself. 
    If a value is provided, the function will check to see if the argument cred_file is set to True or False (default).

    If cred_file is True, then the sa_creds should be a filepath to the service account credentials.  
    If cred_file is False, then sa_creds should be the name of an environment variable in which the credentials are stored. 

    '''
    if gcs_secret_name is not None:
        gcs_secret_path = f"{secret_store}/{gcs_secret_name}"
    secret = os.getenv(secret_env_var)
    if secret is None:
        secret = get_gcs_secret(gcs_secret_path, sa_creds, cred_file)
        os.environ[secret_env_var] = secret
    return(secret)


def get_gcs_secret(secret_name, sa_creds, cred_file):
    from google.cloud import secretmanager
    from google.oauth2 import service_account

    if sa_creds is None:
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


def run_rclone_command(source_path= "", destination_path= "", cmd="copy", client_secret = None, sa_creds = None, gcs_config_name = None, background = False, checkflag = True, cred_file = None):
    
    # GET THE CLIENT SECRET
    if client_secret is None:
        client_secret = get_secret("GCS_CLIENT_SECRET", "gcs_client_secret/versions/1")
        
    if client_secret is None:
        client_secret = os.getenv("GCS_CLIENT_SECRET")

    # GET THE SERVICE ACCOUNT CREDENTIALS
    if cred_file is not None:
        with open(cred_file, "r") as f:
            sa_creds = f.read()
        if len(sa_creds.strip()) == 0:
            sa_creds = None
            
    if sa_creds is None:
        sa_creds = get_secret("GCS_SA", "service-account-cred/versions/1")

    if sa_creds is None:
        sa_creds = os.getenv("GCS_SA")

    # PASS THEM INTO THE RCLONE COMMAND LINE ARGUMENTS
    cmd_args = [
                f"--gcs-client-id=776258882599-v17f82atu67g16na3oiq6ga3bnudoqrh.apps.googleusercontent.com" ,
                f"--gcs-client-secret={client_secret}",
                f"--gcs-project-number=mf-crucible",
                f"--gcs-service-account-credentials='{sa_creds}'",
                f"--gcs-object-acl=projectPrivate",
                f"--gcs-bucket-acl=projectPrivate",
                f"--gcs-env-auth=true"]

    # CLEAN UP THE COMMAND
    if len(destination_path.strip()) > 0:
            destination_path = f'"{destination_path}"'
        
    if cmd_args is not None:
        rclone_cmd = "   ".join([f'rclone {cmd}'] + cmd_args + [f'"{source_path}" {destination_path}'])
    else:
        source_path, destination_path = (x.replace(":gcs", gcs_config_name) for x in (source_path, destination_path))
        rclone_cmd = f'rclone {cmd} "{source_path}" {destination_path}'

    # RUN THE COMMAND
    run_shell_out = run_shell(rclone_cmd, background = background, checkflag=checkflag)
    return(run_shell_out)

def get_tz_isoformat(timezone = "America/Los_Angeles"):
    pst= pytz.timezone(timezone)
    curr_pct_time = datetime.now(pst).isoformat()
    return(curr_pct_time)
