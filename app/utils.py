import os
import subprocess as sp
import json


def run_shell(cmd, checkflag = True, background = False):
    if background:
        return(sp.Popen(cmd, stdout = sp.PIPE, stderr = sp.STDOUT, shell = True, universal_newlines = True))
    return(sp.run(cmd, stdout = sp.PIPE, stderr = sp.STDOUT, shell = True, universal_newlines = True, check = checkflag))


def get_secret_2(env_var, gcs_secret_path= None): 
    secret = os.getenv(env_var)
    if secret is None:
        secret = get_gcs_secret(gcs_secret_path)
        os.environ[env_var] = secret
    return(secret)


def get_gcs_secret(secret_name):
    from google.cloud import secretmanager
    from google.oauth2 import service_account
    try:
        J = json.loads(os.getenv("GCS_SA"))
        with open("temp_creds.json", "w") as f:
            json.dump(J, f)
        credentials = service_account.Credentials.from_service_account_file("temp_creds.json")
        client = secretmanager.SecretManagerServiceClient(credentials=credentials)
        response = client.access_secret_version(name=secret_name)
        secret_value = response.payload.data.decode("UTF-8")
        return(secret_value)
    except Exception as err:
        print(err)
        