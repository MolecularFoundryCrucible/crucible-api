import argparse
import sys

from app.cli import admin, auth, db, files, settings, storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Crucible server administration.")
    groups = parser.add_subparsers(dest="group", required=True)
    db.register(groups)
    admin.register(groups)
    auth.register(groups)
    settings.register(groups)
    storage.register(groups)
    files.register(groups)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
