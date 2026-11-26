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


