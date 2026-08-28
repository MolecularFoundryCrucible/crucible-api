import argparse
import hashlib
import json
from pathlib import Path

from app.api.main import app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("openapi/crucible-api.json"))
    args = parser.parse_args()

    content = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{digest}  {args.output.as_posix()}\n"
    )


if __name__ == "__main__":
    main()
