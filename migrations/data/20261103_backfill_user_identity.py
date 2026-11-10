"""
Moved into the server commands, which also work inside the image:

    python -m app.cli identity backfill --issuer https://orcid.org
    python -m app.cli identity backfill --issuer https://orcid.org --commit

This file forwards to that command so earlier instructions keep working.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.cli.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["identity", "backfill", *sys.argv[1:]]))
