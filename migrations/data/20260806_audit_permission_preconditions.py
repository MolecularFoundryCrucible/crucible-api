"""
Read-only audit run before the permission-system migration (step 0.5 of
docs/permission_system_plan.md). Writes nothing; every query is a SELECT.

Three counts, each gating a decision in the phase-1 backfill:

  1. public-column disagreement   -> which column is authoritative per type
  2. dangling Sample.project_id   -> samples reachable from no project view
  3. resourceacl read/write mix   -> whether the backfill needs conditionals

Run against staging first, then against production immediately before the
real migration. The staging clone drifts from the moment it is taken, so
a clean staging result does not carry over.

Usage (from inside the pod):
    /root/.venv/bin/python migrations/data/20260806_audit_permission_preconditions.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine

SAMPLE_LIMIT = 20


def _rows(session, sql):
    # session.execute, not SQLModel's session.exec — exec() scalarises results
    # and would silently collapse these multi-column selects to one column.
    return session.execute(text(sql)).all()


def check_public_disagreement(session):
    """Count datasets where Dataset.public and Resource.public disagree.

    Each one is a resource whose visibility depends on which route you ask.
    """
    print("\n[1] public-column disagreement (dataset.public vs idtype.public)")

    total = _rows(session, """
        SELECT count(*) FROM dataset d
        JOIN idtype r ON r.mfid = d.unique_id
        WHERE d.public IS DISTINCT FROM r.public
    """)[0][0]

    print(f"    disagreeing datasets: {total}")

    if total == 0:
        print("    -> rule is moot; migrate from either column")
        return total

    breakdown = _rows(session, """
        SELECT d.public AS dataset_public, r.public AS resource_public, count(*)
        FROM dataset d JOIN idtype r ON r.mfid = d.unique_id
        WHERE d.public IS DISTINCT FROM r.public
        GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    for ds_pub, res_pub, n in breakdown:
        direction = "would PUBLISH" if ds_pub else "would HIDE"
        print(f"    dataset.public={ds_pub!s:<5} idtype.public={res_pub!s:<5} "
              f"n={n:<6} taking dataset.public {direction} vs today's idtype view")

    sample = _rows(session, f"""
        SELECT d.unique_id, d.dataset_name, d.public, r.public
        FROM dataset d JOIN idtype r ON r.mfid = d.unique_id
        WHERE d.public IS DISTINCT FROM r.public
        LIMIT {SAMPLE_LIMIT}
    """)
    print(f"    first {len(sample)}:")
    for mfid, name, ds_pub, res_pub in sample:
        print(f"      {mfid}  dataset={ds_pub!s:<5} idtype={res_pub!s:<5}  {name}")

    return total


def check_dangling_sample_projects(session):
    """Count samples naming a project that does not exist.

    Sample.project_id has no FK. Must be zero before one can be added.
    """
    print("\n[2] dangling Sample.project_id (no FK constraint on this column)")

    total = _rows(session, """
        SELECT count(*) FROM sample s
        LEFT JOIN project p ON p.project_id = s.project_id
        WHERE s.project_id IS NOT NULL AND p.project_id IS NULL
    """)[0][0]

    print(f"    samples naming a nonexistent project: {total}")

    if total == 0:
        print("    -> safe to add the FK in schema hygiene item 2")
        return total

    offenders = _rows(session, f"""
        SELECT s.project_id, count(*)
        FROM sample s LEFT JOIN project p ON p.project_id = s.project_id
        WHERE s.project_id IS NOT NULL AND p.project_id IS NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT {SAMPLE_LIMIT}
    """)
    print("    by project_id value:")
    for proj_id, n in offenders:
        print(f"      {proj_id!r:<40} {n}")

    return total


def check_acl_read_write_mix(session):
    """Check the read/write distribution.

    The backfill is a uniform assignment only if every row is (t,t) apart from
    read_only. POST /resources/{mfid}/access_groups takes caller-supplied
    values, so this confirms nothing else got in.
    """
    print("\n[3] resourceacl read/write distribution")

    rows = _rows(session, """
        SELECT read, write, count(*) FROM resourceacl GROUP BY 1, 2 ORDER BY 3 DESC
    """)
    for read, write, n in rows:
        print(f"    read={read!s:<5} write={write!s:<5} n={n}")

    unexpected = [(r, w, n) for r, w, n in rows if not (r and w) and not (r and not w)]
    if unexpected:
        print("    -> UNEXPECTED combinations present; the backfill needs conditional logic:")
        for r, w, n in unexpected:
            print(f"       read={r} write={w} n={n}")
    else:
        print("    -> only (t,t) and (t,f); backfill needs no conditional logic")

    write_only = _rows(session, """
        SELECT count(*) FROM resourceacl WHERE write = true AND read = false
    """)[0][0]
    if write_only:
        print(f"    -> {write_only} write-without-read rows: these have no role "
              f"equivalent, since every role above viewer implies read")

    return rows


def check_duplicate_group_owners(session):
    """Find ORCID groups with more than one member — each would get two `owner`
    standings and break one_owner_per_group."""
    print("\n[4] groups that would end up with more than one owner")

    # User.unique_id is the Python attribute; the column is literally `orcid`
    # (core_models.py:201, sa_column=Column("orcid", ...)). Raw SQL needs the
    # column name.
    dupes = _rows(session, f"""
        SELECT ag.group_name, count(*) AS members
        FROM useraccessgrouplink l
        JOIN accessgroup ag ON ag.id = l.accessgroup_id
        JOIN "user" u ON u.id = l.user_id
        WHERE ag.group_name = u.orcid
        GROUP BY ag.group_name HAVING count(*) > 1
        ORDER BY 2 DESC LIMIT {SAMPLE_LIMIT}
    """)

    if not dupes:
        print("    no ORCID group has more than one member")
    else:
        print(f"    {len(dupes)} ORCID groups with multiple members "
              f"(each would get two `owner` standings):")
        for name, n in dupes:
            print(f"      {name:<40} {n} members")

    return dupes


def check_empty_mfid_resources(session):
    print("\n[5] resources with an empty MFID")
    rows = _rows(session, """
        SELECT
            resource_type,
            (SELECT count(*) FROM sample WHERE unique_id = '') AS samples,
            (SELECT count(*) FROM dataset WHERE unique_id = '') AS datasets,
            (SELECT count(*) FROM instrument WHERE unique_id = '') AS instruments,
            (SELECT count(*) FROM project WHERE unique_id = '') AS projects,
            (SELECT count(*) FROM resourceacl WHERE mfid = '') AS acl_rows,
            (SELECT count(*) FROM scientific_metadata_new WHERE unique_id = '') AS metadata_rows,
            (SELECT count(*) FROM deletionrequest WHERE resource_id = '') AS deletion_requests
        FROM idtype
        WHERE mfid = ''
    """)
    if not rows:
        print("    no empty-MFID resources")
        return rows

    for row in rows:
        print(
            f"    type={row.resource_type!r} samples={row.samples} datasets={row.datasets} "
            f"instruments={row.instruments} projects={row.projects} acl_rows={row.acl_rows} "
            f"metadata_rows={row.metadata_rows} deletion_requests={row.deletion_requests}"
        )
    print("    -> take the first snapshot, then run the guarded cleanup script")
    return rows


def main():
    engine = get_engine()
    print(f"permission migration precondition audit — {engine.url.database} "
          f"on {engine.url.host}")
    print("read-only; nothing is written")

    with Session(engine) as session:
        # Production and staging are the same instance, differing only by
        # database name, so report what the server says rather than what the
        # config claims.
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r}")
        public_drift = check_public_disagreement(session)
        dangling = check_dangling_sample_projects(session)
        check_acl_read_write_mix(session)
        dupe_owners = check_duplicate_group_owners(session)
        empty_mfids = check_empty_mfid_resources(session)

    print("\n" + "=" * 60)
    blockers = []
    if public_drift:
        blockers.append(f"{public_drift} datasets with ambiguous visibility")
    if dangling:
        blockers.append(f"{dangling} samples naming a nonexistent project")
    if dupe_owners:
        blockers.append(f"{len(dupe_owners)} ORCID groups with multiple members")
    if empty_mfids:
        blockers.append(f"{len(empty_mfids)} resources with an empty MFID")

    if blockers:
        print("RESOLVE BEFORE PHASE 1:")
        for b in blockers:
            print(f"  - {b}")
        return 1

    print("No blockers. Phase 1 backfill can proceed as a uniform assignment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
