#!/usr/bin/env python3
"""
Script to test all Crucible API endpoints.
This script demonstrates how to make requests to each endpoint in the API.

Before running, set these environment variables:
- API_BASE_URL: Base URL of the API (e.g., https://your-domain.com/api)
- API_KEY: Your Crucible API key (get from /user_apikey endpoint)
"""

import os
import json
import mfid
import requests
from typing import Dict, List, Optional

# Configuration
API_BASE_URL = os.getenv("API_BASE_URL", "https://crucible.lbl.gov/testapi2")
API_KEY = os.getenv("API_KEY", "9840ba714e5c03d109acdfbc1baa957525e6cd94fa87800bb018443560807282")

# Headers for authenticated requests
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}


def check_response(endpoint: str, response: requests.Response):
    """Helper function to verify response is not an HTTP error"""
    if response.status_code >= 400:
        print(f"ERROR - {endpoint}: HTTP {response.status_code}")
        try:
            print(f"  Details: {response.json()}")
        except:
            print(f"  Details: {response.text}")
    else:
        print(f"OK - {endpoint}: {response.status_code}")


# ======================= AUTH AND ACCOUNT ENDPOINTS =======================

def test_login():
    """GET /login - Redirect to ORCID login (use in browser)"""
    endpoint = f"{API_BASE_URL}/login"
    print(f"\nBrowser endpoint: {endpoint}")
    # This returns a redirect, so you'd use it in a browser


def test_get_account():
    """GET /account - Get account info"""
    endpoint = f"{API_BASE_URL}/account"
    response = requests.get(endpoint, headers=HEADERS)
    check_response("GET /account", response)
    return response.json() if response.status_code == 200 else None


def test_get_apikey():
    """GET /user_apikey - Get user API key (requires cookie auth)"""
    # This endpoint requires cookie authentication from ORCID login
    # Not easily testable via requests without browser session
    print("\nGET /user_apikey - Requires browser authentication")


def test_regenerate_apikey():
    """GET /regenerate_apikey - Regenerate API key (requires cookie auth)"""
    # This endpoint requires cookie authentication from ORCID login
    print("\nGET /regenerate_apikey - Requires browser authentication")


# ======================= DATASET ENDPOINTS =======================

def test_create_dataset(dataset_data: Dict):
    """POST /datasets - Create a new dataset"""
    endpoint = f"{API_BASE_URL}/datasets"
    response = requests.post(endpoint, headers=HEADERS, json=dataset_data)
    check_response("POST /datasets", response)
    return response.json() if response.status_code == 200 else None


def test_list_datasets(query_params: Optional[Dict] = None):
    """GET /datasets - List all datasets"""
    endpoint = f"{API_BASE_URL}/datasets"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    check_response("GET /datasets", response)
    return response.json() if response.status_code == 200 else None


def test_get_dataset(dsid: str):
    """GET /datasets/{dsid} - Get a specific dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}", response)
    return response.json() if response.status_code == 200 else None


def test_update_dataset(dsid: str, update_data: Dict):
    """PATCH /datasets/{dsid} - Update a dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}"
    response = requests.patch(endpoint, headers=HEADERS, json=update_data)
    check_response(f"PATCH /datasets/{dsid}", response)
    return response.json() if response.status_code == 200 else None


def test_delete_dataset(dsid: str):
    """DELETE /datasets/{dsid} - Delete a dataset (not implemented)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}"
    response = requests.delete(endpoint, headers=HEADERS)
    check_response(f"DELETE /datasets/{dsid}", response)


def test_download_dataset_file(dsid: str, file_name: str, save_path: str):
    """GET /datasets/{dsid}/download/{file_name} - Download a file"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/download/{file_name}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/download/{file_name}", response)

    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)


def test_upload_dataset_file(dsid: str, file_path: str):
    """POST /datasets/{dsid}/upload - Upload files to dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/upload"

    with open(file_path, 'rb') as f:
        files = {'files': f}
        headers_no_content_type = {"Authorization": f"Bearer {API_KEY}"}
        response = requests.post(endpoint, headers=headers_no_content_type, files=files)

    check_response(f"POST /datasets/{dsid}/upload", response)
    return response.json() if response.status_code == 200 else None


def test_request_ingestion(dsid: str, file_to_upload: Optional[str] = None, ingestion_class: Optional[str] = None):
    """POST /datasets/{dsid}/ingest - Request dataset ingestion"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/ingest"
    params = {}
    if file_to_upload:
        params['file_to_upload'] = file_to_upload
    if ingestion_class:
        params['ingestion_class'] = ingestion_class

    response = requests.post(endpoint, headers=HEADERS, params=params)
    check_response(f"POST /datasets/{dsid}/ingest", response)
    return response.json() if response.status_code == 200 else None


def test_get_ingestion_request(dsid: str, reqid: int):
    """GET /datasets/{dsid}/ingest/{reqid} - Get ingestion request status"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/ingest/{reqid}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/ingest/{reqid}", response)
    return response.json() if response.status_code == 200 else None


def test_patch_ingestion_request(dsid: str, reqid: int, request_data: Dict):
    """PATCH /datasets/{dsid}/ingest/{reqid} - Update ingestion request (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/ingest/{reqid}"
    response = requests.patch(endpoint, headers=HEADERS, json=request_data)
    check_response(f"PATCH /datasets/{dsid}/ingest/{reqid}", response)


def test_request_google_drive_transfer(dsid: str):
    """POST /datasets/{dsid}/google_drive_transfer - Request transfer to Google Drive"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/google_drive_transfer"
    response = requests.post(endpoint, headers=HEADERS)
    check_response(f"POST /datasets/{dsid}/google_drive_transfer", response)
    return response.json() if response.status_code == 200 else None


def test_patch_transfer_request(dsid: str, reqid: int, request_data: Dict):
    """PATCH /datasets/{dsid}/google_drive_transfer/{reqid} - Update transfer request (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/google_drive_transfer/{reqid}"
    response = requests.patch(endpoint, headers=HEADERS, json=request_data)
    check_response(f"PATCH /datasets/{dsid}/google_drive_transfer/{reqid}", response)


def test_request_scicat_update(dsid: str, overwrite_data: bool = False):
    """POST /datasets/{dsid}/scicat_update - Request SciCat update"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scicat_update"
    params = {"overwrite_data": overwrite_data}
    response = requests.post(endpoint, headers=HEADERS, params=params)
    check_response(f"POST /datasets/{dsid}/scicat_update", response)
    return response.json() if response.status_code == 200 else None


def test_get_scicat_update_request(dsid: str, reqid: int):
    """GET /datasets/{dsid}/scicat_update/{reqid} - Get SciCat update request status"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scicat_update/{reqid}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/scicat_update/{reqid}", response)


def test_patch_scicat_request(dsid: str, reqid: int, request_data: Dict):
    """PATCH /datasets/{dsid}/scicat_update/{reqid} - Update SciCat request (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scicat_update/{reqid}"
    response = requests.patch(endpoint, headers=HEADERS, json=request_data)
    check_response(f"PATCH /datasets/{dsid}/scicat_update/{reqid}", response)


# ======================= SCIENTIFIC METADATA ENDPOINTS =======================

def test_get_scientific_metadata(dsid: str):
    """GET /datasets/{dsid}/scientific_metadata - Get scientific metadata"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scientific_metadata"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/scientific_metadata", response)
    return response.json() if response.status_code == 200 else None


def test_add_scientific_metadata(dsid: str, metadata: Dict):
    """POST /datasets/{dsid}/scientific_metadata - Add scientific metadata"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scientific_metadata"
    response = requests.post(endpoint, headers=HEADERS, json=metadata)
    check_response(f"POST /datasets/{dsid}/scientific_metadata", response)
    return response.json() if response.status_code == 200 else None


def test_update_scientific_metadata(dsid: str, metadata_updates: Dict):
    """PATCH /datasets/{dsid}/scientific_metadata - Update scientific metadata"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scientific_metadata"
    response = requests.patch(endpoint, headers=HEADERS, json=metadata_updates)
    check_response(f"PATCH /datasets/{dsid}/scientific_metadata", response)
    return response.json() if response.status_code == 200 else None


# ======================= THUMBNAIL ENDPOINTS =======================

def test_get_thumbnails(dsid: str):
    """GET /datasets/{dsid}/thumbnails - Get thumbnails"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/thumbnails"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/thumbnails", response)
    return response.json() if response.status_code == 200 else None


def test_add_thumbnail(dsid: str, thumbnail_data: Dict):
    """POST /datasets/{dsid}/thumbnails - Add thumbnail"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/thumbnails"
    response = requests.post(endpoint, headers=HEADERS, json=thumbnail_data)
    check_response(f"POST /datasets/{dsid}/thumbnails", response)
    return response.json() if response.status_code == 200 else None


# ======================= ASSOCIATED FILES ENDPOINTS =======================

def test_get_associated_files(dsid: str):
    """GET /datasets/{dsid}/associated_files - Get associated files"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/associated_files"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/associated_files", response)
    return response.json() if response.status_code == 200 else None


def test_add_associated_file(dsid: str, file_data: Dict):
    """POST /datasets/{dsid}/associated_files - Add associated file"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/associated_files"
    response = requests.post(endpoint, headers=HEADERS, json=file_data)
    check_response(f"POST /datasets/{dsid}/associated_files", response)
    return response.json() if response.status_code == 200 else None


# ======================= KEYWORD ENDPOINTS =======================

def test_get_keywords_for_dataset(dsid: str):
    """GET /datasets/{dsid}/keywords - List keywords for dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/keywords"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/keywords", response)
    return response.json() if response.status_code == 200 else None


def test_add_keyword_to_dataset(dsid: str, keyword: str):
    """POST /datasets/{dsid}/keywords - Add keyword to dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/keywords"
    params = {"keyword": keyword}
    response = requests.post(endpoint, headers=HEADERS, params=params)
    check_response(f"POST /datasets/{dsid}/keywords", response)
    return response.json() if response.status_code == 200 else None


def test_read_keyword(keyword: str, permissive: bool = True):
    """GET /keywords/{keyword} - Search keywords"""
    endpoint = f"{API_BASE_URL}/keywords/{keyword}"
    params = {"permissive": permissive}
    response = requests.get(endpoint, headers=HEADERS, params=params)
    check_response(f"GET /keywords/{keyword}", response)
    return response.json() if response.status_code == 200 else None


# ======================= DRIVE LOCATION ENDPOINTS =======================

def test_get_drive_location(dsid: str):
    """GET /datasets/{dsid}/drive_location - Get Google Drive locations"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/drive_location"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/drive_location", response)
    return response.json() if response.status_code == 200 else None


def test_create_drive_location(dsid: str, drive_info: Dict):
    """POST /datasets/{dsid}/drive_location - Add Drive location"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/drive_location"
    response = requests.post(endpoint, headers=HEADERS, json=drive_info)
    check_response(f"POST /datasets/{dsid}/drive_location", response)
    return response.json() if response.status_code == 200 else None


# ======================= ACCESS GROUP ENDPOINTS =======================

def test_read_dataset_acl(dsid: str):
    """GET /datasets/{dsid}/access_groups - Get dataset ACL (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/access_groups"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/access_groups", response)
    return response.json() if response.status_code == 200 else None


def test_create_dataset_acl(dsid: str, group_name: str, read: bool, write: bool):
    """POST /datasets/{dsid}/access_groups - Create dataset ACL (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/access_groups"
    params = {"group_name": group_name, "dsid": dsid, "read": read, "write": write}
    response = requests.post(endpoint, headers=HEADERS, params=params)
    check_response(f"POST /datasets/{dsid}/access_groups", response)


def test_create_access_group(access_group: Dict):
    """POST /access_groups - Create access group (admin only)"""
    endpoint = f"{API_BASE_URL}/access_groups"
    response = requests.post(endpoint, headers=HEADERS, json=access_group)
    check_response("POST /access_groups", response)


def test_read_access_group(group_name: str):
    """GET /access_groups/{group_name} - Get access group (admin only)"""
    endpoint = f"{API_BASE_URL}/access_groups/{group_name}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /access_groups/{group_name}", response)


def test_add_user_to_access_group(orcid: str, group_name: str):
    """POST /users/{orcid}/access_groups/{group_name} - Add user to group (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/access_groups/{group_name}"
    response = requests.post(endpoint, headers=HEADERS)
    check_response(f"POST /users/{orcid}/access_groups/{group_name}", response)


def test_list_user_access_groups(orcid: str):
    """GET /users/{orcid}/access_groups - List user's access groups"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/access_groups"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /users/{orcid}/access_groups", response)


# ======================= DATASET COLLECTION ENDPOINTS =======================

def test_create_dataset_collection(collection_data: Dict):
    """POST /dataset_collections - Create dataset collection"""
    endpoint = f"{API_BASE_URL}/dataset_collections"
    response = requests.post(endpoint, headers=HEADERS, json=collection_data)
    check_response("POST /dataset_collections", response)


def test_get_dataset_collection(dcid: str):
    """GET /dataset_collections/{dcid} - Get dataset collection (admin only)"""
    endpoint = f"{API_BASE_URL}/dataset_collections/{dcid}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /dataset_collections/{dcid}", response)


def test_list_dataset_collections(query_params: Optional[Dict] = None):
    """GET /dataset_collections - List dataset collections (admin only)"""
    endpoint = f"{API_BASE_URL}/dataset_collections"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    check_response("GET /dataset_collections", response)


# ======================= INSTRUMENT ENDPOINTS =======================

def test_list_instruments(query_params: Optional[Dict] = None):
    """GET /instruments - List instruments"""
    endpoint = f"{API_BASE_URL}/instruments"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    check_response("GET /instruments", response)
    return response.json() if response.status_code == 200 else None


def test_get_instrument(instrument_unique_id: str):
    """GET /instruments/{instrument_unique_id} - Get instrument"""
    endpoint = f"{API_BASE_URL}/instruments/{instrument_unique_id}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /instruments/{instrument_unique_id}", response)
    return response.json() if response.status_code == 200 else None


def test_create_instrument(instrument_data: Dict):
    """POST /instruments - Create instrument"""
    endpoint = f"{API_BASE_URL}/instruments"
    response = requests.post(endpoint, headers=HEADERS, json=instrument_data)
    check_response("POST /instruments", response)


# ======================= GOOGLE DRIVE ENDPOINTS =======================

def test_list_google_drives(query_params: Optional[Dict] = None):
    """GET /google_drives - List Google Drives"""
    endpoint = f"{API_BASE_URL}/google_drives"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    check_response("GET /google_drives", response)


def test_get_google_drive(drive_name: str):
    """GET /google_drives/{drive_name} - Get Google Drive info"""
    endpoint = f"{API_BASE_URL}/google_drives/{drive_name}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /google_drives/{drive_name}", response)


def test_create_google_drive(drive_data: Dict):
    """POST /google_drives - Create Google Drive info"""
    endpoint = f"{API_BASE_URL}/google_drives"
    response = requests.post(endpoint, headers=HEADERS, json=drive_data)
    check_response("POST /google_drives", response)


# ======================= USER ENDPOINTS =======================

def test_list_users(query_params: Optional[Dict] = None, permissive: bool = True):
    """GET /users - List users (admin only)"""
    endpoint = f"{API_BASE_URL}/users"
    params = query_params or {}
    params['permissive'] = permissive
    response = requests.get(endpoint, headers=HEADERS, params=params)
    check_response("GET /users", response)


def test_get_user(orcid: str):
    """GET /users/{orcid} - Get user (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /users/{orcid}", response)


def test_create_user(user_data: Dict, project_ids: List[str]):
    """POST /users - Create user (admin only)"""
    endpoint = f"{API_BASE_URL}/users"
    data = {**user_data, "project_ids": project_ids}
    response = requests.post(endpoint, headers=HEADERS, json=data)
    check_response("POST /users", response)


def test_read_user_datasets(orcid: str):
    """GET /users/{orcid}/datasets - Get user's datasets (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/datasets"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /users/{orcid}/datasets", response)


def test_read_user_access_to_dataset(orcid: str, dsid: str):
    """GET /users/{orcid}/datasets/{dsid} - Check user access (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/datasets/{dsid}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /users/{orcid}/datasets/{dsid}", response)


def test_read_projects_for_user(orcid: str):
    """GET /users/{orcid}/projects - Get user's projects (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/projects"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /users/{orcid}/projects", response)


# ======================= PROJECT ENDPOINTS =======================

def test_create_project(project_data: Dict):
    """POST /projects - Create project"""
    endpoint = f"{API_BASE_URL}/projects"
    response = requests.post(endpoint, headers=HEADERS, json=project_data)
    check_response("POST /projects", response)


def test_list_projects():
    """GET /projects - List projects (admin only)"""
    endpoint = f"{API_BASE_URL}/projects"
    response = requests.get(endpoint, headers=HEADERS)
    check_response("GET /projects", response)


def test_get_project(proj_id: str):
    """GET /projects/{proj_id} - Get project"""
    endpoint = f"{API_BASE_URL}/projects/{proj_id}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /projects/{proj_id}", response)


def test_read_users_on_project(proj_id: str):
    """GET /projects/{proj_id}/users - Get users on project (admin only)"""
    endpoint = f"{API_BASE_URL}/projects/{proj_id}/users"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /projects/{proj_id}/users", response)


# ======================= SAMPLE ENDPOINTS =======================

def test_create_sample(sample_data: Dict):
    """POST /samples - Create sample"""
    endpoint = f"{API_BASE_URL}/samples"
    response = requests.post(endpoint, headers=HEADERS, json=sample_data)
    check_response("POST /samples", response)
    return response.json() if response.status_code == 200 else None


def test_link_parent_child_samples(parent_sample_id: str, child_sample_id: str):
    """POST /samples/{parent_sample_id}/children/{child_sample_id} - Link samples"""
    endpoint = f"{API_BASE_URL}/samples/{parent_sample_id}/children/{child_sample_id}"
    response = requests.post(endpoint, headers=HEADERS)
    check_response(f"POST /samples/{parent_sample_id}/children/{child_sample_id}", response)


def test_add_sample_to_dataset(dsid: str, sample_id: str):
    """POST /datasets/{dsid}/samples/{sample_id} - Link sample to dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/samples/{sample_id}"
    response = requests.post(endpoint, headers=HEADERS)
    check_response(f"POST /datasets/{dsid}/samples/{sample_id}", response)


def test_list_samples(query_params: Optional[Dict] = None):
    """GET /samples - List samples"""
    endpoint = f"{API_BASE_URL}/samples"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    check_response("GET /samples", response)
    return response.json() if response.status_code == 200 else None


def test_get_sample(sample_id: str):
    """GET /samples/{sample_id} - Get sample"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /samples/{sample_id}", response)
    return response.json() if response.status_code == 200 else None


def test_get_samples_in_dataset(dsid: str):
    """GET /datasets/{dsid}/samples - Get samples in dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/samples"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /datasets/{dsid}/samples", response)


def test_list_sample_datasets(sample_id: str):
    """GET /samples/{sample_id}/datasets - Get datasets for sample"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}/datasets"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /samples/{sample_id}/datasets", response)


def test_list_child_samples(parent_sample_id: str):
    """GET /samples/{parent_sample_id}/children - Get child samples"""
    endpoint = f"{API_BASE_URL}/samples/{parent_sample_id}/children"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /samples/{parent_sample_id}/children", response)


def test_get_sample_metadata(sample_id: str, sample_type: str):
    """GET /samples/{sample_id}/metadata/{sample_type} - Get sample metadata"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}/metadata/{sample_type}"
    response = requests.get(endpoint, headers=HEADERS)
    check_response(f"GET /samples/{sample_id}/metadata/{sample_type}", response)


def test_create_sample_metadata(sample_id: str, sample_type: str, metadata: Dict):
    """POST /samples/{sample_id}/metadata/{sample_type} - Create sample metadata"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}/metadata/{sample_type}"
    response = requests.post(endpoint, headers=HEADERS, json=metadata)
    check_response(f"POST /samples/{sample_id}/metadata/{sample_type}", response)


# ======================= MAIN DEMONSTRATION =======================

def main():
    """
    Comprehensive test of all API endpoints.

    Required arguments (provide these when running the script):
    - dsid: Dataset ID (e.g., "ds-123456")
    - orcid: User ORCID ID (e.g., "0000-0001-2345-6789")
    - reqid: Request ID for ingestion/transfer/scicat operations (integer, e.g., 1)
    - keyword: Keyword string (e.g., "microscopy")
    - group_name: Access group name (e.g., "test-group")
    - dcid: Dataset collection ID (e.g., "dc-123")
    - instrument_unique_id: Instrument ID (e.g., "instrument-001")
    - drive_name: Google Drive name (e.g., "my-drive")
    - proj_id: Project ID (e.g., "proj-123")
    - sample_id: Sample ID (e.g., "sample-001")
    - parent_sample_id: Parent sample ID (e.g., "parent-sample-001")
    - child_sample_id: Child sample ID (e.g., "child-sample-001")
    - sample_type: Sample type (e.g., "biological")
    - file_name: File name for download (e.g., "data.txt")
    - file_path: Local file path for upload (e.g., "/path/to/file.txt")
    - save_path: Path to save downloaded files (e.g., "/tmp/downloaded.txt")
    """
    print("Crucible API Endpoint Test Script - Comprehensive Test Suite")
    print("=" * 80)

    # ======================= CONFIGURE TEST PARAMETERS =======================
    # TODO: Set these values before running tests
    dsid = "0t0003pr6dzmz000mfsp74j6w0"  # e.g., "ds-123456"
    orcid = "0009-0001-9493-2006"  # e.g., "0000-0001-2345-6789"
    ingest_reqid = 6801  # Request ID for various operations
    scicat_reqid = 60
    transfer_reqid = 49
    keyword = "CrBr3"
    group_name = "read_only"
    dcid = "0szb3sskm5v0x000e6v70449gm"
    instrument_unique_id = "0szb5en16nxdk000dcd8a1wt2w"
    drive_name = "MFP08540"
    proj_id = "MFP08540"
    sample_id = "0t3q9hmnr1xtb0006wsy2msf04"
    parent_sample_id = "0t3h7ymbm5s27000z6tt82zvx4"
    child_sample_id = "0t4tndz6pxz4b000dp2sfr75gw"
    sample_type = "spinbot_batch"
    file_name = "eenl_tio2_uv_0129.zip"
    file_path = "tests/example.txt"
    save_path = "/tmp/eenl_tio2_uv_0129.zip"
    measurement = "picam_readout"  # For dataset query filtering
    instrument_name = "team05"  # For dataset query filtering

    # ======================= AUTH AND ACCOUNT ENDPOINTS =======================
    print("\n### TESTING AUTH AND ACCOUNT ENDPOINTS ###")
    test_login()
    test_get_account()
    test_get_apikey()
    test_regenerate_apikey()

    # ======================= DATASET ENDPOINTS =======================
    print("\n### TESTING DATASET ENDPOINTS ###")

    # List datasets - test with different query parameters
    test_list_datasets()  # No query params
    test_list_datasets(query_params={"measurement": measurement})  # Filter by measurement
    test_list_datasets(query_params={"instrument_name": instrument_name, "owner_orcid": orcid})  # Multiple filters

    # Get specific dataset
    test_get_dataset(dsid)

    # Create dataset
    uuid = mfid.mfid()[0]
    dataset_data = {
        "unique_id": uuid,
        "dataset_name": "Test Dataset",
        "description": "A test dataset created by automated test script",
        "public": False
    }
    test_create_dataset(dataset_data)

    # Update dataset
    update_data = {
        "dataset_name": "Updated Test Dataset",
        "description": "Updated description"
    }
    test_update_dataset(dsid, update_data)

    # File operations
    test_download_dataset_file(dsid, file_name, save_path)
    test_upload_dataset_file(dsid, file_path)

    # Ingestion
    test_request_ingestion(dsid)
    test_request_ingestion(dsid, file_to_upload="data.h5", ingestion_class="HDF5Ingestion")
    test_get_ingestion_request(dsid, ingest_reqid)
    test_patch_ingestion_request(dsid, ingest_reqid, {"status": "completed"})

    # Google Drive transfer
    test_request_google_drive_transfer(dsid)
    test_patch_transfer_request(dsid, transfer_reqid, {"status": "completed"})

    # SciCat update
    test_request_scicat_update(dsid, overwrite_data=False)
    test_get_scicat_update_request(dsid, scicat_reqid)
    test_patch_scicat_request(dsid, scicat_reqid, {"status": "completed"})

    # ======================= SCIENTIFIC METADATA ENDPOINTS =======================
    print("\n### TESTING SCIENTIFIC METADATA ENDPOINTS ###")
    test_get_scientific_metadata(dsid)

    metadata = {
        "temperature": {"value": 298, "unit": "K"},
        "pressure": {"value": 1.0, "unit": "atm"}
    }
    test_add_scientific_metadata(dsid, metadata)

    metadata_updates = {
        "temperature": {"value": 300, "unit": "K"}
    }
    test_update_scientific_metadata(dsid, metadata_updates)

    # ======================= THUMBNAIL ENDPOINTS =======================
    print("\n### TESTING THUMBNAIL ENDPOINTS ###")
    test_get_thumbnails(dsid)

    # Thumbnail requires: thumbnail_name (str) and thumbnail_b64str (str)
    thumbnail_data = {
        "thumbnail_name": "Test Thumbnail",
        "thumbnail_b64str": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    }
    test_add_thumbnail(dsid, thumbnail_data)

    # ======================= ASSOCIATED FILES ENDPOINTS =======================
    print("\n### TESTING ASSOCIATED FILES ENDPOINTS ###")
    test_get_associated_files(dsid)

    # AssociatedFile requires: filename (str), size (int), and sha256_hash (str)
    import hashlib
    file_data = {
        "filename": "example.txt",
        "size": 1024,
        "sha256_hash": hashlib.sha256(b"test file content").hexdigest()
    }
    test_add_associated_file(dsid, file_data)

    # ======================= KEYWORD ENDPOINTS =======================
    print("\n### TESTING KEYWORD ENDPOINTS ###")
    test_get_keywords_for_dataset(dsid)
    test_add_keyword_to_dataset(dsid, keyword)
    test_read_keyword(keyword, permissive=True)
    test_read_keyword(keyword, permissive=False)

    # ======================= DRIVE LOCATION ENDPOINTS =======================
    print("\n### TESTING DRIVE LOCATION ENDPOINTS ###")
    test_get_drive_location(dsid)

    drive_info = {
        "drive_name": drive_name,
        "folder_id": "google-drive-folder-id"
    }
    # test_create_drive_location(dsid, drive_info)

    # ======================= ACCESS GROUP ENDPOINTS =======================
    print("\n### TESTING ACCESS GROUP ENDPOINTS ###")
    test_read_dataset_acl(dsid)
   # test_create_dataset_acl(dsid, group_name, read=True, write=False)

    # access_group = {
    #     "group_name": group_name,
    #     "description": "Test access group"
    # }
    #test_create_access_group(access_group)
    test_read_access_group(group_name)

    #test_add_user_to_access_group(orcid, group_name)
    test_list_user_access_groups(orcid)

    # ======================= DATASET COLLECTION ENDPOINTS =======================
    print("\n### TESTING DATASET COLLECTION ENDPOINTS ###")
    uuid = mfid.mfid()[0]
    collection_data = {
        "unique_id": uuid,
        "collection_name": "Test Collection",
        "description": "A test dataset collection"
    }
    test_create_dataset_collection(collection_data)
    test_get_dataset_collection(dcid)
    test_list_dataset_collections()
    test_list_dataset_collections(query_params={"limit": 10})

    # ======================= INSTRUMENT ENDPOINTS =======================
    print("\n### TESTING INSTRUMENT ENDPOINTS ###")
    test_list_instruments()
    test_list_instruments(query_params={"limit": 10})
    test_get_instrument(instrument_unique_id)

    instrument_data = {
        "unique_id": instrument_unique_id,
        "instrument_name": "Test Instrument",
        "description": "A test instrument"
    }
    test_create_instrument(instrument_data)

    # ======================= GOOGLE DRIVE ENDPOINTS =======================
    print("\n### TESTING GOOGLE DRIVE ENDPOINTS ###")
    test_list_google_drives()
    test_list_google_drives(query_params={"limit": 10})
    test_get_google_drive(drive_name)

    drive_data = {
        "drive_name": drive_name,
        "drive_id": "google-drive-id",
        "description": "Test Google Drive"
    }
    test_create_google_drive(drive_data)

    # ======================= USER ENDPOINTS =======================
    print("\n### TESTING USER ENDPOINTS ###")
    test_list_users()
    test_list_users(query_params={"limit": 10}, permissive=True)
    test_get_user(orcid)

    user_data = {
        "orcid": orcid,
        "name": "Test User",
        "email": "test@example.com"
    }
    #test_create_user(user_data, project_ids=[proj_id])

    test_read_user_datasets(orcid)
    test_read_user_access_to_dataset(orcid, dsid)
    test_read_projects_for_user(orcid)

    # ======================= PROJECT ENDPOINTS =======================
    print("\n### TESTING PROJECT ENDPOINTS ###")
    
    new_proj_id = 'TEST000002'
    project_data = {
        "project_id": new_proj_id,
        "organization": "Molecular Foundry",
        "project_lead_email": "mkwall@lbl.gov"
    }
    test_create_project(project_data)
    test_list_projects()
    test_get_project(proj_id)
    test_read_users_on_project(proj_id)

    # ======================= SAMPLE ENDPOINTS =======================
    print("\n### TESTING SAMPLE ENDPOINTS ###")
    test_list_samples()
    test_list_samples(query_params={"limit": 10})
    test_get_sample(sample_id)
    uuid = mfid.mfid()[0]
    sample_data = {
        "unique_id": uuid,
        "sample_name": "Test Sample",
        "description": "A test sample"
    }
    test_create_sample(sample_data)

    # Sample relationships
    test_link_parent_child_samples(parent_sample_id, child_sample_id)
    test_add_sample_to_dataset(dsid, sample_id)
    test_get_samples_in_dataset(dsid)
    test_list_sample_datasets(sample_id)
    test_list_child_samples(parent_sample_id)

    # Sample metadata
    test_get_sample_metadata(sample_id, sample_type)
    sample_metadata = {
        "spin_duration_s": 30,
        "spin_velocity_rpm": 600, 
        "dispense_delay_s": 3,
        "pipette_height_mm": 0.2, 
        "dispense_speed_ul_s": 40,
        "precursor_b_volume_ul": 80,
        "annealing_duration_s": 300,
        "molar_ratio_fai_macl": "5:1"
    }
    test_create_sample_metadata(sample_id, sample_type, sample_metadata)

    # ======================= COMPLETION =======================
    print("\n" + "=" * 80)
    print("Test script completed!")
    print("\nNote: Many endpoints require specific data or admin privileges.")
    print("Make sure to update the test parameters at the top of main() before running.")
    print("\nSome tests may fail if:")
    print("  - You don't have admin privileges")
    print("  - The specified IDs don't exist")
    print("  - You don't have proper access permissions")
    print("  - Required files don't exist at specified paths")


if __name__ == "__main__":
    main()
