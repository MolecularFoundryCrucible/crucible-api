import argparse
import json
import sys


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("files", help="Manage stored files").add_subparsers(dest="command", required=True)
    restore = commands.add_parser(
        "restore",
        help="Make a file's previous content current again",
        description="Restores the newest kept version of a replaced file (within FILE_VERSION_RETENTION_DAYS of the replace). The content it replaces is kept as a version, so running it again undoes the restore.",
    )
    restore.add_argument("file_mfid", metavar="FILE_MFID")
    restore.set_defaults(handler=_run_restore)
    backfill = commands.add_parser(
        "backfill-locations",
        help="Record the path and generation of files stored before multipart uploads",
        description="Pass 1: for files without a storage path, finds their object in the store \"default\" (the exact key {dataset}/{filename}, else the one object ending in the file's basename) and records its path and generation. With --generations, pass 2: records the generation of files that have a path in that store's bucket but none, so cleanup may delete their content once they are replaced or deleted. Files where several objects or none match are listed and left. Reports only, unless --commit; each row is written only if it has not changed since it was read. Exits 1 when any file could not be located.",
    )
    backfill.add_argument("--commit", action="store_true", help="Write what was found")
    backfill.add_argument("--generations", action="store_true", help="Also record the generation of files that have a path")
    backfill.add_argument("--batch", type=int, default=500, help="Rows read at a time (default 500)")
    backfill.add_argument("--workers", type=int, default=8, help="Parallel object lookups in pass 2 (default 8)")
    backfill.set_defaults(handler=_run_backfill)


def _run_restore(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.config import check_settings
    from app.services.file_versions import RestoreRefused, restore_latest
    from app.services.multipart import now
    from app.storage import get_store

    try:
        check_settings()
        restored = restore_latest(get_engine(), get_store, args.file_mfid, now())
    except RestoreRefused as error:
        print(f"Not restored: {error.code}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Not restored: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"file_mfid": restored.file_mfid, "storage_path": restored.storage_path, "restored_version": restored.version_id, "kept_version": restored.replaced_version_id}))
    return 0


def _run_backfill(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.config import check_settings
    from app.services.location_backfill import backfill_locations
    from app.storage import get_store

    try:
        check_settings()
        report = backfill_locations(get_engine(), get_store, commit=args.commit, generations=args.generations, batch=args.batch, workers=args.workers)
    except Exception as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    verb = "recorded" if args.commit else "would record"
    print(f"{verb} {report.paths} paths and {report.generations} generations; {len(report.ambiguous)} ambiguous, {len(report.missing)} not found, {len(report.changed)} changed meanwhile")
    for mfid in report.ambiguous:
        print(f"  ambiguous: {mfid}")
    for mfid in report.missing:
        print(f"  not found: {mfid}")
    for mfid in report.changed:
        print(f"  changed meanwhile, left as it is: {mfid}")
    return 1 if report.ambiguous or report.missing else 0
