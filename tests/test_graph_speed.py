#!/usr/bin/env python3
"""
Speed comparison between project-query-based graph endpoints (new) and
CTE/BFS-based graph endpoints (old).

Usage:
    python test_graph_speed.py --sample-ids ID1 ID2 ... --dataset-ids ID1 ID2 ...
    python test_graph_speed.py  # auto-fetches IDs from the API

Environment variables (or .env file):
    API_BASE_URL  - e.g. http://localhost:7777
    admin_apikey  - API key with admin access
"""

import os
import sys
import time
import argparse
import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:7777")
API_KEY = os.getenv("CRUCIBLE_API_KEY")

if not API_KEY:
    sys.exit("ERROR: admin_apikey not set in environment or .env file")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def timed_get(url: str, n: int) -> dict:
    """Call url n times, return min/mean/max wall-clock seconds."""
    times = []
    status = None
    for _ in range(n):
        t0 = time.perf_counter()
        r = requests.get(url, headers=HEADERS)
        times.append(time.perf_counter() - t0)
        status = r.status_code
    if status and status >= 400:
        return {"error": status, "min": None, "mean": None, "max": None}
    return {
        "error": None,
        "min": min(times),
        "mean": sum(times) / len(times),
        "max": max(times),
    }


def fmt(result: dict) -> str:
    if result["error"]:
        return f"HTTP {result['error']}"
    return f"min={result['min']*1000:.0f}ms  mean={result['mean']*1000:.0f}ms  max={result['max']*1000:.0f}ms"


def print_row(label_a: str, result_a: dict, label_b: str, result_b: dict):
    print(f"  {label_a:<12} {fmt(result_a)}")
    print(f"  {label_b:<12} {fmt(result_b)}")
    if result_a["mean"] and result_b["mean"]:
        ratio = result_a["mean"] / result_b["mean"]
        winner = label_b if ratio > 1 else label_a
        print(f"  winner: {winner} ({max(ratio, 1/ratio):.2f}x faster)")


def fetch_sample_ids(n: int = 3) -> list[str]:
    r = requests.get(f"{API_BASE_URL}/samples", headers=HEADERS, params={"limit": n})
    r.raise_for_status()
    return [s["unique_id"] for s in r.json()[:n]]


def fetch_dataset_ids(n: int = 3) -> list[str]:
    r = requests.get(f"{API_BASE_URL}/datasets", headers=HEADERS, params={"limit": n})
    r.raise_for_status()
    return [d["unique_id"] for d in r.json()[:n]]


def run(sample_ids: list[str], dataset_ids: list[str], repeats: int):
    print(f"\nAPI: {API_BASE_URL}")
    print(f"Repeats per endpoint per ID: {repeats}")
    print("=" * 70)

    # --- sample_graph ---
    print(f"\n{'─'*70}")
    print("SAMPLE GRAPH  (/samples/<id>/sample_graph)")
    for sid in sample_ids:
        print(f"\n  sample: {sid}")
        new = timed_get(f"{API_BASE_URL}/samples/{sid}/sample_graph", repeats)
        cte = timed_get(f"{API_BASE_URL}/samples/{sid}/sample_graph_cte", repeats)
        print_row("project", new, "cte", cte)

    # --- dataset_graph ---
    print(f"\n{'─'*70}")
    print("DATASET GRAPH  (/datasets/<id>/dataset_graph)")
    for did in dataset_ids:
        print(f"\n  dataset: {did}")
        new = timed_get(f"{API_BASE_URL}/datasets/{did}/dataset_graph", repeats)
        cte = timed_get(f"{API_BASE_URL}/datasets/{did}/dataset_graph_cte", repeats)
        print_row("project", new, "cte", cte)

    # --- entity_graph (sample root) ---
    print(f"\n{'─'*70}")
    print("ENTITY GRAPH — sample root  (/entity_graph/<id>)")
    for sid in sample_ids:
        print(f"\n  sample: {sid}")
        new = timed_get(f"{API_BASE_URL}/entity_graph/{sid}", repeats)
        bfs = timed_get(f"{API_BASE_URL}/entity_graph_bfs/{sid}", repeats)
        print_row("project", new, "bfs", bfs)

    # --- entity_graph (dataset root) ---
    print(f"\n{'─'*70}")
    print("ENTITY GRAPH — dataset root  (/entity_graph/<id>)")
    for did in dataset_ids:
        print(f"\n  dataset: {did}")
        new = timed_get(f"{API_BASE_URL}/entity_graph/{did}", repeats)
        bfs = timed_get(f"{API_BASE_URL}/entity_graph_bfs/{did}", repeats)
        print_row("project", new, "bfs", bfs)

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Graph endpoint speed comparison")
    parser.add_argument("--sample-ids", nargs="+", help="Sample unique_ids to test")
    parser.add_argument("--dataset-ids", nargs="+", help="Dataset unique_ids to test")
    parser.add_argument("--repeats", type=int, default=5, help="Calls per endpoint per ID (default 5)")
    args = parser.parse_args()

    sample_ids = args.sample_ids or fetch_sample_ids(3)
    dataset_ids = args.dataset_ids or fetch_dataset_ids(3)

    if not sample_ids:
        sys.exit("No sample IDs available — pass --sample-ids or ensure /samples returns data")
    if not dataset_ids:
        sys.exit("No dataset IDs available — pass --dataset-ids or ensure /datasets returns data")

    run(sample_ids, dataset_ids, args.repeats)
