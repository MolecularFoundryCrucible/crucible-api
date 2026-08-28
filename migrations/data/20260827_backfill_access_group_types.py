"""Audit and classify access groups by their owning entity or reserved purpose.

Dry-run audit works before the nullable group_type column exists. A committing run requires revision 91f3c8a4d2e7. Classification uses exact canonical identifiers: User.unique_id identifies identity groups for both humans and service accounts, Project.unique_id identifies project groups, and Instrument.unique_id identifies instrument groups. The reserved public, admin, and read_only groups are system groups. Every unmatched group is reported as custom and requires --accept-custom-groups on a committing run. Legacy-looking custom groups also require --accept-suspicious-custom-groups so their cleanup is deferred explicitly.

The script refuses ambiguous identifiers, existing conflicting classifications, and malformed identity-group membership. It is idempotent, defaults to a dry run, and requires an explicit production acknowledgement.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_backfill_access_group_types.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_backfill_access_group_types.py --commit --accept-custom-groups --accept-suspicious-custom-groups
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_backfill_access_group_types.py --commit --accept-custom-groups --accept-suspicious-custom-groups --yes-this-is-production
"""
import argparse
from collections import Counter
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import inspect, text
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType, Role


PRODUCTION_DB = "crucible"
SYSTEM_GROUPS = {"admin", "public", "read_only"}
ORCID_PATTERN = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.IGNORECASE)


def classify_group_name(
    group_name: str,
    user_ids: set[str],
    project_ids: set[str],
    instrument_ids: set[str],
) -> tuple[str | None, tuple[str, ...]]:
    matches = []
    if group_name in SYSTEM_GROUPS:
        matches.append(AccessGroupType.SYSTEM.value)
    if group_name in user_ids:
        matches.append(AccessGroupType.IDENTITY.value)
    if group_name in project_ids:
        matches.append(AccessGroupType.PROJECT.value)
    if group_name in instrument_ids:
        matches.append(AccessGroupType.INSTRUMENT.value)
    if len(matches) > 1:
        return None, tuple(matches)
    if matches:
        return matches[0], tuple(matches)
    return AccessGroupType.CUSTOM.value, ()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="apply changes; otherwise roll back")
    parser.add_argument(
        "--accept-custom-groups",
        action="store_true",
        help="confirm that unmatched existing groups should be classified as custom",
    )
    parser.add_argument(
        "--accept-suspicious-custom-groups",
        action="store_true",
        help="defer cleanup of legacy-looking custom groups without changing their access",
    )
    parser.add_argument(
        "--yes-this-is-production",
        action="store_true",
        help=f"required to write to the {PRODUCTION_DB!r} database",
    )
    args = parser.parse_args()

    engine = get_engine()
    columns = {column["name"] for column in inspect(engine).get_columns("accessgroup")}
    has_group_type = "group_type" in columns
    if args.commit and not has_group_type:
        print("REFUSING TO RUN: accessgroup.group_type does not exist.")
        print("Apply Alembic revision 91f3c8a4d2e7 first.")
        return 2

    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")
        print(f"group_type    : {'present' if has_group_type else 'not installed; audit only'}")

        if database == PRODUCTION_DB and args.commit and not args.yes_this_is_production:
            print(f"REFUSING TO RUN: {database!r} is production.")
            print("Re-run with --yes-this-is-production if that is intended.")
            return 2

        entity_ids = session.execute(text("""
            SELECT 'identity' AS group_type, orcid AS unique_id FROM "user"
            UNION ALL
            SELECT 'project' AS group_type, unique_id FROM project WHERE unique_id IS NOT NULL
            UNION ALL
            SELECT 'instrument' AS group_type, unique_id FROM instrument WHERE unique_id IS NOT NULL
            UNION ALL
            SELECT 'legacy_project' AS group_type, project_id FROM project WHERE project_id IS NOT NULL
            UNION ALL
            SELECT 'legacy_instrument' AS group_type, lower(instrument_name) FROM instrument
        """)).all()
        group_type_column = ", group_type::text AS group_type" if has_group_type else ", NULL::text AS group_type"
        groups = session.execute(text(
            f"SELECT id, group_name{group_type_column} FROM accessgroup"
        )).all()

        user_ids = {entity.unique_id for entity in entity_ids if entity.group_type == AccessGroupType.IDENTITY.value}
        project_ids = {entity.unique_id for entity in entity_ids if entity.group_type == AccessGroupType.PROJECT.value}
        instrument_ids = {
            entity.unique_id for entity in entity_ids if entity.group_type == AccessGroupType.INSTRUMENT.value
        }
        legacy_project_ids = {
            entity.unique_id for entity in entity_ids if entity.group_type == "legacy_project"
        }
        legacy_instrument_names = {
            entity.unique_id for entity in entity_ids if entity.group_type == "legacy_instrument"
        }

        classified = {}
        ambiguous = []
        for group in groups:
            group_type, matches = classify_group_name(
                group.group_name,
                user_ids,
                project_ids,
                instrument_ids,
            )
            if group_type is None:
                ambiguous.append((group.group_name, matches))
            else:
                classified[group.id] = group_type

        if ambiguous:
            print(f"ambiguous     : {len(ambiguous)}")
            for group_name, matches in ambiguous[:20]:
                print(f"  {group_name!r}: {', '.join(matches)}")
            print("REFUSING TO RUN: resolve identifier collisions before classification.")
            return 2

        conflicting = []
        for group in groups:
            expected = classified[group.id]
            if group.group_type is not None and group.group_type != expected:
                conflicting.append((group.group_name, group.group_type, expected))
        if conflicting:
            print(f"conflicting   : {len(conflicting)}")
            for group_name, current, expected in conflicting[:20]:
                print(f"  {group_name!r}: stored={current!r}, expected={expected!r}")
            print("REFUSING TO RUN: existing classifications are not overwritten automatically.")
            return 2

        groups_by_name = {group.group_name: group for group in groups}
        missing_entity_groups = []
        entity_sets = (
            (AccessGroupType.IDENTITY.value, user_ids),
            (AccessGroupType.PROJECT.value, project_ids),
            (AccessGroupType.INSTRUMENT.value, instrument_ids),
        )
        for expected_type, identifiers in entity_sets:
            for identifier in identifiers:
                group = groups_by_name.get(identifier)
                if group is None:
                    missing_entity_groups.append((expected_type, identifier))
        if PUBLIC_GROUP_NAME not in groups_by_name:
            missing_entity_groups.append((AccessGroupType.SYSTEM.value, PUBLIC_GROUP_NAME))
        if missing_entity_groups:
            print(f"missing groups : {len(missing_entity_groups)}")
            for group_type, identifier in missing_entity_groups[:20]:
                print(f"  {group_type}: {identifier!r}")
            print("REFUSING TO RUN: provision missing entity-owned groups before classification.")
            return 2

        identity_issues = session.execute(text("""
            SELECT
                u.orcid AS group_name,
                COALESCE(
                    json_agg(
                        json_build_array(link.user_id, link.role::text)
                        ORDER BY link.user_id
                    ) FILTER (WHERE link.user_id IS NOT NULL),
                    '[]'::json
                ) AS memberships
            FROM "user" AS u
            JOIN accessgroup AS access_group ON access_group.group_name = u.orcid
            LEFT JOIN useraccessgrouplink AS link ON link.accessgroup_id = access_group.id
            GROUP BY u.id, u.orcid
            HAVING
                count(link.user_id) <> 1
                OR count(link.user_id) FILTER (
                    WHERE link.user_id = u.id AND link.role::text = :owner_role
                ) <> 1
            ORDER BY u.orcid
        """), {"owner_role": Role.OWNER.db_value}).all()

        if identity_issues:
            print(f"identity issues: {len(identity_issues)}")
            for group_name, memberships in identity_issues[:20]:
                print(f"  {group_name!r}: memberships={memberships!r}")
            print("REFUSING TO RUN: identity groups must contain only their owner at owner standing.")
            return 2

        counts = Counter(classified.values())
        custom_names = sorted(
            group.group_name
            for group in groups
            if classified[group.id] == AccessGroupType.CUSTOM.value
        )
        for group_type in AccessGroupType:
            print(f"{group_type.value:14}: {counts[group_type.value]}")
        if custom_names:
            print("custom sample : " + ", ".join(repr(name) for name in custom_names[:20]))

        suspicious = []
        for group_name in custom_names:
            reasons = []
            if group_name in legacy_project_ids:
                reasons.append("legacy project_id")
            if group_name.lower() in legacy_instrument_names:
                reasons.append("legacy instrument_name")
            if ORCID_PATTERN.fullmatch(group_name):
                reasons.append("ORCID-shaped orphan")
            if reasons:
                suspicious.append((group_name, reasons))
        if suspicious:
            print(f"suspicious custom groups: {len(suspicious)}")
            for group_name, reasons in suspicious[:50]:
                print(f"  {group_name!r}: {', '.join(reasons)}")
            print("warning: these groups require case-by-case cleanup after migration")

        if args.commit and custom_names and not args.accept_custom_groups:
            print("REFUSING TO COMMIT: review custom groups and pass --accept-custom-groups.")
            return 2
        if args.commit and suspicious and not args.accept_suspicious_custom_groups:
            print(
                "REFUSING TO COMMIT: pass --accept-suspicious-custom-groups "
                "to defer their cleanup without changing access."
            )
            return 2

        if not has_group_type:
            print(f"rows to classify after expansion: {len(groups)}")
            session.rollback()
            print("audit only: apply revision 91f3c8a4d2e7 before committing")
            return 0

        pending = sum(group.group_type is None for group in groups)
        print(f"rows to update: {pending}")
        if not args.commit:
            session.rollback()
            print("dry run only: nothing written")
            return 0

        update_statements = (
            (AccessGroupType.SYSTEM.value, "group_name IN ('admin', 'public', 'read_only')"),
            (
                AccessGroupType.IDENTITY.value,
                'EXISTS (SELECT 1 FROM "user" WHERE "user".orcid = accessgroup.group_name)',
            ),
            (
                AccessGroupType.PROJECT.value,
                "EXISTS (SELECT 1 FROM project WHERE project.unique_id = accessgroup.group_name)",
            ),
            (
                AccessGroupType.INSTRUMENT.value,
                "EXISTS (SELECT 1 FROM instrument WHERE instrument.unique_id = accessgroup.group_name)",
            ),
        )
        changed = 0
        for group_type, predicate in update_statements:
            result = session.execute(text(f"""
                UPDATE accessgroup
                SET group_type = CAST(:group_type AS access_group_type)
                WHERE group_type IS NULL AND {predicate}
            """), {"group_type": group_type})
            changed += result.rowcount
        result = session.execute(text("""
            UPDATE accessgroup
            SET group_type = CAST(:group_type AS access_group_type)
            WHERE group_type IS NULL
        """), {"group_type": AccessGroupType.CUSTOM.value})
        changed += result.rowcount

        if {"owner_orcid", "resource_mfid"}.issubset(columns):
            session.execute(text("""
                UPDATE accessgroup ag
                SET owner_orcid = u.orcid,
                    resource_mfid = NULL
                FROM "user" u
                WHERE ag.group_name = u.orcid
            """))
            session.execute(text("""
                UPDATE accessgroup ag
                SET owner_orcid = NULL,
                    resource_mfid = p.project_id
                FROM project p
                WHERE ag.group_name = p.unique_id
            """))
            session.execute(text("""
                UPDATE accessgroup ag
                SET owner_orcid = NULL,
                    resource_mfid = i.unique_id
                FROM instrument i
                WHERE ag.group_name = i.unique_id
            """))

        if changed != pending:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected to update {pending} rows, but updated {changed}.")
            return 2
        session.commit()
        print("committed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
