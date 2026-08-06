#!/usr/bin/env python3
"""
Compare the node/edge output of the project-based entity_graph endpoint
against the BFS-based entity_graph_bfs endpoint for a given entity ID.

Usage:
    python test_graph_diff.py 0ta8e3bhp9vb10001gg4zm6par
    python test_graph_diff.py <entity_id> [--base-url http://localhost:7777]
"""

import sys
import argparse
import requests
from dotenv import load_dotenv
import os

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:7777")
API_KEY = os.getenv("CRUCIBLE_API_KEY")

if not API_KEY:
    sys.exit("ERROR: CRUCIBLE_API_KEY not set in environment or .env file")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def fetch_graph(url: str) -> dict:
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        sys.exit(f"ERROR {r.status_code} from {url}: {r.text}")
    return r.json()


def parse_graph(data: dict) -> tuple[dict, set]:
    """Return (nodes_by_id, edges_set) from node_link_data format."""
    nodes = {n["id"]: {k: v for k, v in n.items() if k != "id"} for n in data.get("nodes", [])}
    edges = {(e["source"], e["target"]) for e in data.get("links", [])}
    return nodes, edges


def compare(entity_id: str, base_url: str):
    url_new = f"{base_url}/entity_graph/{entity_id}"
    url_bfs = f"{base_url}/entity_graph_bfs/{entity_id}"

    print(f"Fetching {url_new} ...")
    new_data = fetch_graph(url_new)
    print(f"Fetching {url_bfs} ...")
    bfs_data = fetch_graph(url_bfs)

    new_nodes, new_edges = parse_graph(new_data)
    bfs_nodes, bfs_edges = parse_graph(bfs_data)

    new_ids = set(new_nodes)
    bfs_ids = set(bfs_nodes)

    print(f"\n{'='*60}")
    print(f"Entity ID : {entity_id}")
    print(f"{'='*60}")
    print(f"  project-based : {len(new_ids)} nodes, {len(new_edges)} edges")
    print(f"  bfs-based     : {len(bfs_ids)} nodes, {len(bfs_edges)} edges")

    only_new = new_ids - bfs_ids
    only_bfs = bfs_ids - new_ids
    common = new_ids & bfs_ids

    print(f"\n  Common nodes  : {len(common)}")

    if only_new:
        print(f"\n  In project-based ONLY ({len(only_new)}):")
        for uid in sorted(only_new):
            attrs = new_nodes[uid]
            print(f"    {uid}  {attrs}")
    else:
        print(f"\n  In project-based ONLY : none")

    if only_bfs:
        print(f"\n  In bfs-based ONLY ({len(only_bfs)}):")
        for uid in sorted(only_bfs):
            attrs = bfs_nodes[uid]
            print(f"    {uid}  {attrs}")
    else:
        print(f"\n  In bfs-based ONLY : none")

    edge_only_new = new_edges - bfs_edges
    edge_only_bfs = bfs_edges - new_edges

    if edge_only_new:
        print(f"\n  Edges in project-based ONLY ({len(edge_only_new)}):")
        for src, tgt in sorted(edge_only_new):
            print(f"    {src} -> {tgt}")

    if edge_only_bfs:
        print(f"\n  Edges in bfs-based ONLY ({len(edge_only_bfs)}):")
        for src, tgt in sorted(edge_only_bfs):
            print(f"    {src} -> {tgt}")

    if not only_new and not only_bfs and not edge_only_new and not edge_only_bfs:
        print("\n  Results are IDENTICAL.")
    else:
        print("\n  Results DIFFER.")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diff two entity_graph implementations")
    parser.add_argument("entity_id", help="Sample or dataset unique_id")
    parser.add_argument("--base-url", default=API_BASE_URL)
    args = parser.parse_args()

    compare(args.entity_id, args.base_url)
