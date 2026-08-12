"""
Backfill instrument.instrument_id — a validated, human-readable handle,
slugified from instrument_name — for every existing instrument.

Purely additive groundwork (see the schema migration, migrations/versions/
20260810_..._add_instrument_instrument_id.py). Idempotent — only touches
rows where instrument_id IS NULL, safe to re-run.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260810_backfill_instrument_id.py
    DB_CONF_FILE=.env python migrations/data/20260810_backfill_instrument_id.py --commit
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Instrument

PRODUCTION_DB = "crucible"

_NON_SLUG = re.compile(r'[^a-z0-9]+')


def slugify(instrument_name: str, fallback: str) -> str:
    slug = _NON_SLUG.sub('-', instrument_name.lower()).strip('-')[:64]
    return slug or fallback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="apply; without it everything is rolled back")
    parser.add_argument("--yes-this-is-production", action="store_true",
                        help=f"required to run against the {PRODUCTION_DB!r} database")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (rolled back)'}")

        if actual == PRODUCTION_DB and not args.yes_this_is_production:
            print(f"\nREFUSING TO RUN: {actual!r} is production.")
            print("Re-run with --yes-this-is-production if that is genuinely intended.")
            return 2

        instruments = session.exec(select(Instrument).where(Instrument.instrument_id == None)).all()  # noqa: E711
        print(f"instruments missing instrument_id: {len(instruments)}")

        seen = set(session.exec(
            select(Instrument.instrument_id).where(Instrument.instrument_id != None)  # noqa: E711
        ).all())
        for inst in instruments:
            base = slugify(inst.instrument_name, fallback=inst.unique_id)
            slug, n = base, 2
            while slug in seen:
                slug = f"{base}-{n}"
                n += 1
            seen.add(slug)
            inst.instrument_id = slug
            session.add(inst)
            print(f"  {inst.instrument_name!r} -> {slug!r}")

        nulls_remaining = session.exec(select(Instrument).where(Instrument.instrument_id == None)).all()  # noqa: E711
        assert not nulls_remaining, f"{len(nulls_remaining)} still null after backfill"

        dupes = session.execute(text(
            "SELECT instrument_id, count(*) FROM instrument WHERE instrument_id IS NOT NULL "
            "GROUP BY instrument_id HAVING count(*) > 1"
        )).all()
        assert not dupes, f"duplicate instrument_id values would result: {dupes}"

        if args.commit:
            session.commit()
            print(f"committed: {len(instruments)} instruments backfilled")
        else:
            session.rollback()
            print(f"dry run only: {len(instruments)} instruments would be backfilled, nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
