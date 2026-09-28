"""Run the durable multipart queue with the API's database configuration."""
import argparse
import logging
import time

from app.api.dependencies import get_engine
from app.services.multipart import MultipartWorker
from app.services.multipart_storage import get_multipart_storage
from app.utils.multipart_config import MultipartStorageConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one due session")
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    logging.basicConfig(level=logging.INFO)
    worker = MultipartWorker(get_engine(), get_multipart_storage(), MultipartStorageConfig.from_env())
    while True:
        try:
            worked = worker.run_once()
        except Exception:
            logging.error("Multipart worker iteration failed; retrying after lease recovery")
            worked = False
        if args.once:
            break
        if not worked:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
