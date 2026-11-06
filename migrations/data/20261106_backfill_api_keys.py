"""
Copy every API key into api_key as its hash, with expires_at one year after it was created, so no
key's life changes. Only inserts, and is safe to run again. Run after the code that writes both
tables is deployed, so keys created by older pods during the rollout are copied too.

Usage (from inside the pod):
    /root/.venv/bin/python migrations/data/20261106_backfill_api_keys.py
    /root/.venv/bin/python migrations/data/20261106_backfill_api_keys.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.auth.api_keys import created_at_of, key_hash, store_key
from app.models.core_models import ApiKey, ApiToken

BATCH_SIZE = 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy API keys into api_key as hashes. A dry run unless --commit is given.")
    parser.add_argument("--commit", action="store_true", help="Write the rows; without it nothing is changed")
    args = parser.parse_args(argv)
    counts = dict.fromkeys(("copied", "already", "no_owner", "unreadable_date"), 0)
    after = ""
    with Session(get_engine()) as session:
        while True:
            tokens = session.exec(select(ApiToken).where(ApiToken.token > after).order_by(ApiToken.token).limit(BATCH_SIZE)).all()
            if not tokens:
                break
            after = tokens[-1].token
            known = set(session.exec(select(ApiKey.key_hash).where(ApiKey.key_hash.in_([key_hash(token.token) for token in tokens]))).all())
            for token in tokens:
                if token.user_id is None:
                    counts["no_owner"] += 1
                    continue
                if key_hash(token.token) in known:
                    counts["already"] += 1
                    continue
                created = created_at_of(token.token_created)
                if created is None:
                    counts["unreadable_date"] += 1
                    print(f"unreadable token_created for a key of user {token.user_id}: {token.token_created!r}", file=sys.stderr)
                    continue
                counts["copied"] += 1
                if args.commit:
                    row = store_key(session, token.user_id, token.token, created_at=created)
                    row.last_used_at = token.last_used_at
            if args.commit:
                session.commit()
    verb = "copied" if args.commit else "dry run, would copy"
    print(f"{verb}: {counts['copied']}, already copied: {counts['already']}, no owner (skipped): {counts['no_owner']}, unreadable date (skipped): {counts['unreadable_date']}")
    return 1 if counts["unreadable_date"] else 0


if __name__ == "__main__":
    sys.exit(main())
