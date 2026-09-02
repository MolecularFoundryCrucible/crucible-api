"""visibility_filter_for narrows a listing by the public ACL entry, not the
legacy `public` column — the whole point per docs/permission_system_plan.md
(Core Concepts -> Public resources). These seed a dataset/sample with no
`public` column value at all, so a filter reading that column would find
nothing.
"""
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import AccessGroup, Dataset, ResourceACL, Sample
from app.utils.permissions import AccessGroupType, PUBLIC_GROUP_NAME
from app.utils.sql_utils import visibility_filter_for


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[
        AccessGroup.__table__, ResourceACL.__table__, Dataset.__table__, Sample.__table__,
    ])
    return engine


def _grant_public(session, mfid, pub_id):
    session.add(ResourceACL(mfid=mfid, accessgroup_id=pub_id, read=True, write=False, permission="viewer"))
    session.commit()


def test_public_and_private_datasets_split_correctly():
    engine = _engine()
    with Session(engine) as session:
        pub = AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type=AccessGroupType.SYSTEM.value)
        session.add(pub)
        session.commit()
        session.refresh(pub)

        session.add(Dataset(unique_id="ds_pub", dataset_name="public one", owner_orcid="0000-0001"))
        session.add(Dataset(unique_id="ds_priv", dataset_name="private one", owner_orcid="0000-0001"))
        session.commit()
        _grant_public(session, "ds_pub", pub.id)

        public_ids = {d.unique_id for d in session.exec(
            select(Dataset).where(visibility_filter_for(Dataset, "public"))).all()}
        private_ids = {d.unique_id for d in session.exec(
            select(Dataset).where(visibility_filter_for(Dataset, "private"))).all()}

        assert public_ids == {"ds_pub"}
        assert private_ids == {"ds_priv"}


def test_samples_use_the_same_predicate():
    engine = _engine()
    with Session(engine) as session:
        pub = AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type=AccessGroupType.SYSTEM.value)
        session.add(pub)
        session.commit()
        session.refresh(pub)

        session.add(Sample(unique_id="s_pub", sample_name="public one", owner_orcid="0000-0001"))
        session.add(Sample(unique_id="s_priv", sample_name="private one", owner_orcid="0000-0001"))
        session.commit()
        _grant_public(session, "s_pub", pub.id)

        public_ids = {s.unique_id for s in session.exec(
            select(Sample).where(visibility_filter_for(Sample, "public"))).all()}

        assert public_ids == {"s_pub"}


def test_no_public_group_at_all_means_everything_is_private():
    """Pre-migration database, or the row was never seeded. Should not raise."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Dataset(unique_id="ds1", dataset_name="x", owner_orcid="0000-0001"))
        session.commit()

        assert session.exec(
            select(Dataset).where(visibility_filter_for(Dataset, "public"))).all() == []
        assert len(session.exec(
            select(Dataset).where(visibility_filter_for(Dataset, "private"))).all()) == 1
