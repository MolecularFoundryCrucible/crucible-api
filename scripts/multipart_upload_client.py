"""Reference signed-part uploader with a local resume record."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import google_crc32c
import requests


def checksum(data):
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


def scan(path, part_size):
    sha = hashlib.sha256()
    crc = google_crc32c.Checksum()
    parts = []
    before = path.stat()
    with path.open("rb") as stream:
        while data := stream.read(part_size):
            sha.update(data)
            crc.update(data)
            parts.append(checksum(data))
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Source file changed while checksumming")
    return sha.hexdigest(), base64.b64encode(crc.digest()).decode(), parts


class Client:
    def __init__(self, api, token):
        self.api = api.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def request(self, method, path, timeout=60, **kwargs):
        for _ in range(12):
            try:
                response = self.session.request(method, self.api + path, timeout=(10, timeout), allow_redirects=False, **kwargs)
            except requests.RequestException:
                time.sleep(5)
                continue
            if response.status_code in {429, 502, 503, 504}:
                time.sleep(min(60, max(1, int(response.headers.get("Retry-After", "5")))))
                continue
            if response.status_code >= 300:
                raise RuntimeError(f"API request failed with HTTP {response.status_code}")
            return response
        raise RuntimeError("API retry budget exhausted; rerun to resume")


def put(path, part, expected):
    with path.open("rb") as stream:
        stream.seek(part["offset"])
        data = stream.read(part["size"])
    if len(data) != part["size"] or checksum(data) != expected:
        raise RuntimeError("Source file changed; abort this session and start a new one")
    for _ in range(3):
        try:
            response = requests.put(part["url"], data=data, headers=part["headers"], timeout=(10, 120), allow_redirects=False)
        except requests.RequestException:
            continue
        if response.status_code == 200:
            return
        if response.status_code < 500:
            break
    raise RuntimeError("Part upload failed; rerun to renew URLs and resume")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="API base URL including its root path")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--token-env", default="CRUCIBLE_API_TOKEN")
    parser.add_argument("--state", type=Path, required=True, help="Local JSON resume file")
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"Set the API token in {args.token_env}")
    if not args.file.is_file():
        parser.error("--file must be a regular file")
    client = Client(args.api, token)
    base = f"/datasets/{args.dataset}/upload/multipart"
    part_size = 64 * 1024 ** 2
    sha, crc, checksums = scan(args.file, part_size)
    identity = {"api": args.api.rstrip("/"), "dataset": args.dataset, "filename": args.file.name, "size": args.file.stat().st_size, "sha256_hash": sha}
    if args.state.exists():
        record = json.loads(args.state.read_text())
        if record["source"] != identity:
            raise RuntimeError("Resume record does not match this file and destination")
    else:
        record = {"source": identity, "idempotency_key": uuid4().hex}
        args.state.write_text(json.dumps(record))
    if "upload_id" not in record:
        result = client.request("POST", base + "/initiate", json={key: identity[key] for key in ("filename", "size", "sha256_hash")}, headers={"Idempotency-Key": record["idempotency_key"]}).json()
        if result["existing_file"]:
            print(json.dumps(result["existing_file"]))
            return
        record["upload_id"] = result["upload_id"]
        args.state.write_text(json.dumps(record))
    identifier = record["upload_id"]
    status = client.request("GET", f"{base}/{identifier}").json()
    if status["state"] == "uploading":
        if status["part_size"] != part_size:
            part_size = status["part_size"]
            _, _, checksums = scan(args.file, part_size)
        if status["part_count"] != len(checksums):
            raise RuntimeError("Unexpected session geometry")
        uploaded = set()
        page = status
        while True:
            uploaded.update(part["part_number"] for part in page["parts"])
            marker = page["next_part_number_marker"]
            if marker is None:
                break
            page = client.request("GET", f"{base}/{identifier}", params={"part_number_marker": marker}).json()
        remaining = [number for number in range(1, len(checksums) + 1) if number not in uploaded]
        for offset in range(0, len(remaining), 32):
            parts = [{"part_number": n, "crc32c": checksums[n - 1]} for n in remaining[offset:offset + 32]]
            signed = client.request("POST", f"{base}/{identifier}/parts", json={"parts": parts}).json()["parts"]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda part: put(args.file, part, checksums[part["part_number"] - 1]), signed))
    elif status["state"] not in {"finalizing", "completed"}:
        raise RuntimeError(f"Upload is {status['state']}: {status.get('failure_code')}")
    response = client.request("POST", base + "/complete", timeout=120, json={"upload_id": identifier, "sha256_hash": sha, "crc32c": crc})
    print(json.dumps(response.json()))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException) as error:
        raise SystemExit(str(error) if isinstance(error, RuntimeError) else "Network failure; rerun to resume") from None
