# EVENTUALLY ALL THESE CAN GET HANDLED THROUGH CONFIG MAP 
gcs_config_name = "mf-cloud-storage"
crucible_api_url = "https://crucible.lbl.gov/testapi"
scicat_url = "https://mf-scicat.lbl.gov/api/v3"
scicat_pw_var = "SCICAT_PROD_PW"
secret_store = "projects/776258882599/secrets"
gcs_client_id = "776258882599-v17f82atu67g16na3oiq6ga3bnudoqrh.apps.googleusercontent.com"
rmq_host =  '10.128.0.31'
rmq_port = 5672
propdb_api_url = "https://foundry-admin.lbl.gov/api/JSON/"

base_export = ['dataset_name',
               'unique_id', 
               'githash',
               'keywords',  
               'creation_time', 
               'size',
               'source_folder',
               'owner', 
               'orcid', 
               'email',
               'user_name', 
               'project_id',
               'principal_investigator',
               'acl',
               'associated_files',
               'metadata_dictionary',
               'google_drive_info',
               'public']

sql_import_attr = [ 'dataset_name', # default is not none
                    'unique_id', 
                    'creation_time', 
                    'size',
                    'source_folder',
                    'project_id',
                    'scientific_metadata',
                    'public','file_to_upload',
                    'measurement',
                    'data_format',
                    'session_name']

sql_export_attr = sql_import_attr + ['thumbnails', 'associated_files', 'keywords', 'acl', 'githash']

# TODO: Don't forget that we should have a field for where the data "lives"

# raw
raw_export_attr =  base_export + [ 'file_to_upload', 
                                   'instrument_name', 
                                   'measurement', 
                                   'local_source_folder',
                                   'creation_location', 
                                   'data_format',
                                   'session_name'
                                ]

raw_import_attr = raw_export_attr  + ['thumbnails']


# derived 
derived_export_attr = base_export + [ 'repo', 
                                      'cloudbuild_link',
                                      'raw_data_uuid', 
                                      'job_parameters', 
                                    ]

# transfers
raw_transfer_import_attr = raw_export_attr + ['thumbnails']
raw_transfer_export_attr = raw_transfer_import_attr

derived_transfer_import_attr = derived_export_attr
derived_transfer_export_attr = derived_transfer_import_attr 


default_patch_attr = {"unique_id":['unique_id', 'dataset_unique_id'], 
                      "metadata_dictionary":['scientific_metadata', 'metadata_dictionary'], 
                      "session_name":["session_name","session"], 
                      "project_id": ["proposal", "project_id"], 
                      "acl": ["acl", "access_groups"] }

default_skip_attr = ["thumbnails"]

ENVMAP = {
          "production": {"storage_bucket":":gcs:/mf-storage-prod"},
          "development": {"storage_bucket":":gcs:/mf-storage-test-1"}
          }
