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
import requests
from typing import Dict, List, Optional

# Configuration
API_BASE_URL = os.getenv("API_BASE_URL", "https://localhost/api")
API_KEY = os.getenv("API_KEY", "your-api-key-here")

# Headers for authenticated requests
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}


def print_response(endpoint: str, response: requests.Response):
    """Helper function to print response details"""
    print(f"\n{'='*80}")
    print(f"Endpoint: {endpoint}")
    print(f"Status Code: {response.status_code}")
    try:
        print(f"Response: {json.dumps(response.json(), indent=2)}")
    except:
        print(f"Response: {response.text}")
    print(f"{'='*80}\n")


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
    print_response("GET /account", response)
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
    print_response("POST /datasets", response)
    return response.json() if response.status_code == 200 else None


def test_list_datasets(query_params: Optional[Dict] = None):
    """GET /datasets - List all datasets"""
    endpoint = f"{API_BASE_URL}/datasets"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    print_response("GET /datasets", response)
    return response.json() if response.status_code == 200 else None


def test_get_dataset(dsid: str):
    """GET /datasets/{dsid} - Get a specific dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}", response)
    return response.json() if response.status_code == 200 else None


def test_update_dataset(dsid: str, update_data: Dict):
    """PATCH /datasets/{dsid} - Update a dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}"
    response = requests.patch(endpoint, headers=HEADERS, json=update_data)
    print_response(f"PATCH /datasets/{dsid}", response)
    return response.json() if response.status_code == 200 else None


def test_delete_dataset(dsid: str):
    """DELETE /datasets/{dsid} - Delete a dataset (not implemented)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}"
    response = requests.delete(endpoint, headers=HEADERS)
    print_response(f"DELETE /datasets/{dsid}", response)


def test_download_dataset_file(dsid: str, file_name: str, save_path: str):
    """GET /datasets/{dsid}/download/{file_name} - Download a file"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/download/{file_name}"
    response = requests.get(endpoint, headers=HEADERS)

    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        print(f"\nFile downloaded to: {save_path}")
    else:
        print_response(f"GET /datasets/{dsid}/download/{file_name}", response)


def test_upload_dataset_file(dsid: str, file_path: str):
    """POST /datasets/{dsid}/upload - Upload files to dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/upload"

    with open(file_path, 'rb') as f:
        files = {'files': f}
        headers_no_content_type = {"Authorization": f"Bearer {API_KEY}"}
        response = requests.post(endpoint, headers=headers_no_content_type, files=files)

    print_response(f"POST /datasets/{dsid}/upload", response)
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
    print_response(f"POST /datasets/{dsid}/ingest", response)
    return response.json() if response.status_code == 200 else None


def test_get_ingestion_request(dsid: str, reqid: int):
    """GET /datasets/{dsid}/ingest/{reqid} - Get ingestion request status"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/ingest/{reqid}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/ingest/{reqid}", response)
    return response.json() if response.status_code == 200 else None


def test_patch_ingestion_request(dsid: str, reqid: int, request_data: Dict):
    """PATCH /datasets/{dsid}/ingest/{reqid} - Update ingestion request (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/ingest/{reqid}"
    response = requests.patch(endpoint, headers=HEADERS, json=request_data)
    print_response(f"PATCH /datasets/{dsid}/ingest/{reqid}", response)


def test_request_google_drive_transfer(dsid: str):
    """POST /datasets/{dsid}/google_drive_transfer - Request transfer to Google Drive"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/google_drive_transfer"
    response = requests.post(endpoint, headers=HEADERS)
    print_response(f"POST /datasets/{dsid}/google_drive_transfer", response)
    return response.json() if response.status_code == 200 else None


def test_patch_transfer_request(dsid: str, reqid: int, request_data: Dict):
    """PATCH /datasets/{dsid}/google_drive_transfer/{reqid} - Update transfer request (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/google_drive_transfer/{reqid}"
    response = requests.patch(endpoint, headers=HEADERS, json=request_data)
    print_response(f"PATCH /datasets/{dsid}/google_drive_transfer/{reqid}", response)


def test_request_scicat_update(dsid: str, overwrite_data: bool = False):
    """POST /datasets/{dsid}/scicat_update - Request SciCat update"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scicat_update"
    params = {"overwrite_data": overwrite_data}
    response = requests.post(endpoint, headers=HEADERS, params=params)
    print_response(f"POST /datasets/{dsid}/scicat_update", response)
    return response.json() if response.status_code == 200 else None


def test_get_scicat_update_request(dsid: str, reqid: int):
    """GET /datasets/{dsid}/scicat_update/{reqid} - Get SciCat update request status"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scicat_update/{reqid}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/scicat_update/{reqid}", response)


def test_patch_scicat_request(dsid: str, reqid: int, request_data: Dict):
    """PATCH /datasets/{dsid}/scicat_update/{reqid} - Update SciCat request (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scicat_update/{reqid}"
    response = requests.patch(endpoint, headers=HEADERS, json=request_data)
    print_response(f"PATCH /datasets/{dsid}/scicat_update/{reqid}", response)


# ======================= SCIENTIFIC METADATA ENDPOINTS =======================

def test_get_scientific_metadata(dsid: str):
    """GET /datasets/{dsid}/scientific_metadata - Get scientific metadata"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scientific_metadata"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/scientific_metadata", response)
    return response.json() if response.status_code == 200 else None


def test_add_scientific_metadata(dsid: str, metadata: Dict):
    """POST /datasets/{dsid}/scientific_metadata - Add scientific metadata"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scientific_metadata"
    response = requests.post(endpoint, headers=HEADERS, json=metadata)
    print_response(f"POST /datasets/{dsid}/scientific_metadata", response)
    return response.json() if response.status_code == 200 else None


def test_update_scientific_metadata(dsid: str, metadata_updates: Dict):
    """PATCH /datasets/{dsid}/scientific_metadata - Update scientific metadata"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/scientific_metadata"
    response = requests.patch(endpoint, headers=HEADERS, json=metadata_updates)
    print_response(f"PATCH /datasets/{dsid}/scientific_metadata", response)
    return response.json() if response.status_code == 200 else None


# ======================= THUMBNAIL ENDPOINTS =======================

def test_get_thumbnails(dsid: str):
    """GET /datasets/{dsid}/thumbnails - Get thumbnails"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/thumbnails"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/thumbnails", response)
    return response.json() if response.status_code == 200 else None


def test_add_thumbnail(dsid: str, thumbnail_data: Dict):
    """POST /datasets/{dsid}/thumbnails - Add thumbnail"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/thumbnails"
    response = requests.post(endpoint, headers=HEADERS, json=thumbnail_data)
    print_response(f"POST /datasets/{dsid}/thumbnails", response)
    return response.json() if response.status_code == 200 else None


# ======================= ASSOCIATED FILES ENDPOINTS =======================

def test_get_associated_files(dsid: str):
    """GET /datasets/{dsid}/associated_files - Get associated files"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/associated_files"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/associated_files", response)
    return response.json() if response.status_code == 200 else None


def test_add_associated_file(dsid: str, file_data: Dict):
    """POST /datasets/{dsid}/associated_files - Add associated file"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/associated_files"
    response = requests.post(endpoint, headers=HEADERS, json=file_data)
    print_response(f"POST /datasets/{dsid}/associated_files", response)
    return response.json() if response.status_code == 200 else None


# ======================= KEYWORD ENDPOINTS =======================

def test_get_keywords_for_dataset(dsid: str):
    """GET /datasets/{dsid}/keywords - List keywords for dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/keywords"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/keywords", response)
    return response.json() if response.status_code == 200 else None


def test_add_keyword_to_dataset(dsid: str, keyword: str):
    """POST /datasets/{dsid}/keywords - Add keyword to dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/keywords"
    params = {"keyword": keyword}
    response = requests.post(endpoint, headers=HEADERS, params=params)
    print_response(f"POST /datasets/{dsid}/keywords", response)
    return response.json() if response.status_code == 200 else None


def test_read_keyword(keyword: str, permissive: bool = True):
    """GET /keywords/{keyword} - Search keywords"""
    endpoint = f"{API_BASE_URL}/keywords/{keyword}"
    params = {"permissive": permissive}
    response = requests.get(endpoint, headers=HEADERS, params=params)
    print_response(f"GET /keywords/{keyword}", response)
    return response.json() if response.status_code == 200 else None


# ======================= DRIVE LOCATION ENDPOINTS =======================

def test_get_drive_location(dsid: str):
    """GET /datasets/{dsid}/drive_location - Get Google Drive locations"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/drive_location"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/drive_location", response)
    return response.json() if response.status_code == 200 else None


def test_create_drive_location(dsid: str, drive_info: Dict):
    """POST /datasets/{dsid}/drive_location - Add Drive location"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/drive_location"
    response = requests.post(endpoint, headers=HEADERS, json=drive_info)
    print_response(f"POST /datasets/{dsid}/drive_location", response)
    return response.json() if response.status_code == 200 else None


# ======================= ACCESS GROUP ENDPOINTS =======================

def test_read_dataset_acl(dsid: str):
    """GET /datasets/{dsid}/access_groups - Get dataset ACL (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/access_groups"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/access_groups", response)
    return response.json() if response.status_code == 200 else None


def test_create_dataset_acl(dsid: str, group_name: str, read: bool, write: bool):
    """POST /datasets/{dsid}/access_groups - Create dataset ACL (admin only)"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/access_groups"
    params = {"group_name": group_name, "dsid": dsid, "read": read, "write": write}
    response = requests.post(endpoint, headers=HEADERS, params=params)
    print_response(f"POST /datasets/{dsid}/access_groups", response)


def test_create_access_group(access_group: Dict):
    """POST /access_groups - Create access group (admin only)"""
    endpoint = f"{API_BASE_URL}/access_groups"
    response = requests.post(endpoint, headers=HEADERS, json=access_group)
    print_response("POST /access_groups", response)


def test_read_access_group(group_name: str):
    """GET /access_groups/{group_name} - Get access group (admin only)"""
    endpoint = f"{API_BASE_URL}/access_groups/{group_name}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /access_groups/{group_name}", response)


def test_add_user_to_access_group(orcid: str, group_name: str):
    """POST /users/{orcid}/access_groups/{group_name} - Add user to group (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/access_groups/{group_name}"
    response = requests.post(endpoint, headers=HEADERS)
    print_response(f"POST /users/{orcid}/access_groups/{group_name}", response)


def test_list_user_access_groups(orcid: str):
    """GET /users/{orcid}/access_groups - List user's access groups"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/access_groups"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /users/{orcid}/access_groups", response)


# ======================= DATASET COLLECTION ENDPOINTS =======================

def test_create_dataset_collection(collection_data: Dict):
    """POST /dataset_collections - Create dataset collection"""
    endpoint = f"{API_BASE_URL}/dataset_collections"
    response = requests.post(endpoint, headers=HEADERS, json=collection_data)
    print_response("POST /dataset_collections", response)


def test_get_dataset_collection(dcid: str):
    """GET /dataset_collections/{dcid} - Get dataset collection (admin only)"""
    endpoint = f"{API_BASE_URL}/dataset_collections/{dcid}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /dataset_collections/{dcid}", response)


def test_list_dataset_collections(query_params: Optional[Dict] = None):
    """GET /dataset_collections - List dataset collections (admin only)"""
    endpoint = f"{API_BASE_URL}/dataset_collections"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    print_response("GET /dataset_collections", response)


# ======================= INSTRUMENT ENDPOINTS =======================

def test_list_instruments(query_params: Optional[Dict] = None):
    """GET /instruments - List instruments"""
    endpoint = f"{API_BASE_URL}/instruments"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    print_response("GET /instruments", response)
    return response.json() if response.status_code == 200 else None


def test_get_instrument(instrument_unique_id: str):
    """GET /instruments/{instrument_unique_id} - Get instrument"""
    endpoint = f"{API_BASE_URL}/instruments/{instrument_unique_id}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /instruments/{instrument_unique_id}", response)
    return response.json() if response.status_code == 200 else None


def test_create_instrument(instrument_data: Dict):
    """POST /instruments - Create instrument"""
    endpoint = f"{API_BASE_URL}/instruments"
    response = requests.post(endpoint, headers=HEADERS, json=instrument_data)
    print_response("POST /instruments", response)


# ======================= GOOGLE DRIVE ENDPOINTS =======================

def test_list_google_drives(query_params: Optional[Dict] = None):
    """GET /google_drives - List Google Drives"""
    endpoint = f"{API_BASE_URL}/google_drives"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    print_response("GET /google_drives", response)


def test_get_google_drive(drive_name: str):
    """GET /google_drives/{drive_name} - Get Google Drive info"""
    endpoint = f"{API_BASE_URL}/google_drives/{drive_name}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /google_drives/{drive_name}", response)


def test_create_google_drive(drive_data: Dict):
    """POST /google_drives - Create Google Drive info"""
    endpoint = f"{API_BASE_URL}/google_drives"
    response = requests.post(endpoint, headers=HEADERS, json=drive_data)
    print_response("POST /google_drives", response)


# ======================= USER ENDPOINTS =======================

def test_list_users(query_params: Optional[Dict] = None, permissive: bool = True):
    """GET /users - List users (admin only)"""
    endpoint = f"{API_BASE_URL}/users"
    params = query_params or {}
    params['permissive'] = permissive
    response = requests.get(endpoint, headers=HEADERS, params=params)
    print_response("GET /users", response)


def test_get_user(orcid: str):
    """GET /users/{orcid} - Get user (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /users/{orcid}", response)


def test_create_user(user_data: Dict, project_ids: List[str]):
    """POST /users - Create user (admin only)"""
    endpoint = f"{API_BASE_URL}/users"
    data = {**user_data, "project_ids": project_ids}
    response = requests.post(endpoint, headers=HEADERS, json=data)
    print_response("POST /users", response)


def test_read_user_datasets(orcid: str):
    """GET /users/{orcid}/datasets - Get user's datasets (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/datasets"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /users/{orcid}/datasets", response)


def test_read_user_access_to_dataset(orcid: str, dsid: str):
    """GET /users/{orcid}/datasets/{dsid} - Check user access (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/datasets/{dsid}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /users/{orcid}/datasets/{dsid}", response)


def test_read_projects_for_user(orcid: str):
    """GET /users/{orcid}/projects - Get user's projects (admin only)"""
    endpoint = f"{API_BASE_URL}/users/{orcid}/projects"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /users/{orcid}/projects", response)


# ======================= PROJECT ENDPOINTS =======================

def test_create_project(project_data: Dict):
    """POST /projects - Create project"""
    endpoint = f"{API_BASE_URL}/projects"
    response = requests.post(endpoint, headers=HEADERS, json=project_data)
    print_response("POST /projects", response)


def test_list_projects():
    """GET /projects - List projects (admin only)"""
    endpoint = f"{API_BASE_URL}/projects"
    response = requests.get(endpoint, headers=HEADERS)
    print_response("GET /projects", response)


def test_get_project(proj_id: str):
    """GET /projects/{proj_id} - Get project"""
    endpoint = f"{API_BASE_URL}/projects/{proj_id}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /projects/{proj_id}", response)


def test_read_users_on_project(proj_id: str):
    """GET /projects/{proj_id}/users - Get users on project (admin only)"""
    endpoint = f"{API_BASE_URL}/projects/{proj_id}/users"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /projects/{proj_id}/users", response)


# ======================= SAMPLE ENDPOINTS =======================

def test_create_sample(sample_data: Dict):
    """POST /samples - Create sample"""
    endpoint = f"{API_BASE_URL}/samples"
    response = requests.post(endpoint, headers=HEADERS, json=sample_data)
    print_response("POST /samples", response)
    return response.json() if response.status_code == 200 else None


def test_link_parent_child_samples(parent_sample_id: str, child_sample_id: str):
    """POST /samples/{parent_sample_id}/children/{child_sample_id} - Link samples"""
    endpoint = f"{API_BASE_URL}/samples/{parent_sample_id}/children/{child_sample_id}"
    response = requests.post(endpoint, headers=HEADERS)
    print_response(f"POST /samples/{parent_sample_id}/children/{child_sample_id}", response)


def test_add_sample_to_dataset(dsid: str, sample_id: str):
    """POST /datasets/{dsid}/samples/{sample_id} - Link sample to dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/samples/{sample_id}"
    response = requests.post(endpoint, headers=HEADERS)
    print_response(f"POST /datasets/{dsid}/samples/{sample_id}", response)


def test_list_samples(query_params: Optional[Dict] = None):
    """GET /samples - List samples"""
    endpoint = f"{API_BASE_URL}/samples"
    response = requests.get(endpoint, headers=HEADERS, params=query_params)
    print_response("GET /samples", response)
    return response.json() if response.status_code == 200 else None


def test_get_sample(sample_id: str):
    """GET /samples/{sample_id} - Get sample"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /samples/{sample_id}", response)
    return response.json() if response.status_code == 200 else None


def test_get_samples_in_dataset(dsid: str):
    """GET /datasets/{dsid}/samples - Get samples in dataset"""
    endpoint = f"{API_BASE_URL}/datasets/{dsid}/samples"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /datasets/{dsid}/samples", response)


def test_list_sample_datasets(sample_id: str):
    """GET /samples/{sample_id}/datasets - Get datasets for sample"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}/datasets"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /samples/{sample_id}/datasets", response)


def test_list_child_samples(parent_sample_id: str):
    """GET /samples/{parent_sample_id}/children - Get child samples"""
    endpoint = f"{API_BASE_URL}/samples/{parent_sample_id}/children"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /samples/{parent_sample_id}/children", response)


def test_get_sample_metadata(sample_id: str, sample_type: str):
    """GET /samples/{sample_id}/metadata/{sample_type} - Get sample metadata"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}/metadata/{sample_type}"
    response = requests.get(endpoint, headers=HEADERS)
    print_response(f"GET /samples/{sample_id}/metadata/{sample_type}", response)


def test_create_sample_metadata(sample_id: str, sample_type: str, metadata: Dict):
    """POST /samples/{sample_id}/metadata/{sample_type} - Create sample metadata"""
    endpoint = f"{API_BASE_URL}/samples/{sample_id}/metadata/{sample_type}"
    response = requests.post(endpoint, headers=HEADERS, json=metadata)
    print_response(f"POST /samples/{sample_id}/metadata/{sample_type}", response)


# ======================= MAIN DEMONSTRATION =======================

def main():
    """
    Demonstration of API endpoints.
    Modify the example data below to match your actual data.
    """
    print("Crucible API Endpoint Test Script")
    print("=" * 80)

    # Test account endpoint
    print("\n### TESTING ACCOUNT ENDPOINT ###")
    account_info = test_get_account()

    # Test dataset listing
    print("\n### TESTING DATASET ENDPOINTS ###")
    datasets = test_list_datasets()

    # Example: Create a dataset (modify this data as needed)
    """
    example_dataset = {
        "unique_id": "test-dataset-001",
        "dataset_name": "Test Dataset",
        "description": "A test dataset",
        "public": False
    }
    created_dataset = test_create_dataset(example_dataset)
    """

    # Test instruments
    print("\n### TESTING INSTRUMENT ENDPOINTS ###")
    instruments = test_list_instruments()

    # Test samples
    print("\n### TESTING SAMPLE ENDPOINTS ###")
    samples = test_list_samples()

    # Example: Create a sample (modify this data as needed)
    """
    example_sample = {
        "unique_id": "test-sample-001",
        "sample_name": "Test Sample",
        "description": "A test sample"
    }
    created_sample = test_create_sample(example_sample)
    """

    # Add more tests as needed...

    print("\n" + "=" * 80)
    print("Test script completed!")
    print("\nNote: Many endpoints require specific data or admin privileges.")
    print("Uncomment and modify the example sections above to test creation endpoints.")


if __name__ == "__main__":
    main()
