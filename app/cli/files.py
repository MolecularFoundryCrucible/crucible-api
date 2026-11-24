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
        help="Record the store, path and generation of files stored before stores were recorded",
        description="Gives every stored file without a recorded store the store \"default\", and finds the storage path and generation of files from before multipart uploads in that store: the exact key {dataset}/{filename}, else the one object ending in the file's basename. Reports files where several objects or none match, and leaves them. Reports only, unless --commit. Exits 1 when any file could not be located.",
    )
    backfill.add_argument("--commit", action="store_true", help="Write the locations found")
    backfill.add_argument("--batch", type=int, default=500, help="Rows read at a time (default 500)")
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
        report = backfill_locations(get_engine(), get_store, commit=args.commit, batch=args.batch)
    except Exception as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    verb = "filled" if args.commit else "would fill"
    print(f"{verb} {report.filled} locations; {report.already} files only needed their store; {len(report.ambiguous)} ambiguous, {len(report.missing)} not found")
    for mfid in report.ambiguous:
        print(f"  ambiguous: {mfid}")
    for mfid in report.missing:
        print(f"  not found: {mfid}")
    for mfid in report.changed:
        print(f"  changed meanwhile, left as it is: {mfid}")
    return 1 if report.ambiguous or report.missing else 0
