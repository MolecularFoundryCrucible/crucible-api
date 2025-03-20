import os
import subprocess as sp
import json


def run_shell(cmd, checkflag = True, background = False):
    if background:
        return(sp.Popen(cmd, stdout = sp.PIPE, stderr = sp.STDOUT, shell = True, universal_newlines = True))
    return(sp.run(cmd, stdout = sp.PIPE, stderr = sp.STDOUT, shell = True, universal_newlines = True, check = checkflag))


def get_secret(secret_env_var, gcs_secret_path = None, sa_creds: str = None, cred_file: bool = False): 
    '''
    sa_creds defaults to None in which case it is assumed that you are running the function from a GCP instance with a service account that can authenticate itself. 
    If a value is provided, the function will check to see if the argument cred_file is set to True or False (default).

    If cred_file is True, then the sa_creds should be a filepath to the service account credentials.  
    If cred_file is False, then sa_creds should be the name of an environment variable in which the credentials are stored. 

    '''
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

        