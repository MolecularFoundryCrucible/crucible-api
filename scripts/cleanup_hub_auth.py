import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.sql_utils import setup_engine
from app.auth.cleanup import cleanup_sign_in_records


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean expired Hub authentication records in bounded batches.")
    parser.add_argument("--commit", action="store_true", help="delete records; otherwise report candidate counts only")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--batches", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 10000 or not 1 <= args.batches <= 1000:
        parser.error("batch-size must be 1..10000 and batches must be 1..1000")
    engine = None
    try:
        engine = setup_engine(env_file=os.getenv("DB_CONF_FILE"))
        if engine is None:
            raise RuntimeError("Database configuration unavailable")
        for _ in range(args.batches if args.commit else 1):
            counts = cleanup_sign_in_records(engine, commit=args.commit, batch_size=args.batch_size)
            print(json.dumps({"mode": "commit" if args.commit else "dry-run", "counts": counts}))
            if not any(counts.values()):
                break
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
