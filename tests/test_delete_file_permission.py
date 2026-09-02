"""DELETE /files/{file_id} requires admin on the parent dataset
(Permission.DELETE), not editor (Permission.EDIT) -- deleting a dataset
itself already requires admin, and deleting one of its files shouldn't be
a lower bar than that. No test previously covered this route at all.
"""
from sqlmodel import Session

from app.models.core_models import AccessGroup, AssociatedFile, Dataset, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import Role


def _setup(db_engine, grantee_role):
    with Session(db_engine) as session:
        user = User(first_name="A", last_name="B", email="a@x.com", unique_id="0000-0001")
        session.add(user)
        ds = Dataset(unique_id="ds_1", dataset_name="d1", owner_orcid="0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)

        ag = AccessGroup(group_name="grp_1")
        session.add(ag)
        session.add(Resource(mfid="ds_1", resource_type="dataset"))
        session.add(AssociatedFile(mfid="file_1", filename="a.txt", storage_path="bucket/ds_1/a.txt",
                                   size=10, sha256_hash="x", dataset_id=ds.id))
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="owner"))
        session.add(ResourceACL(mfid="ds_1", accessgroup_id=ag.id, read=True, write=True,
                                permission=grantee_role.db_value))
        session.commit()
        return ag.id


def test_editor_cannot_delete_a_file(client, db_engine, auth_as):
    ag_id = _setup(db_engine, Role.EDITOR)
    auth_as("0000-0001", groups=[("grp_1", ag_id, Role.EDITOR)])

    resp = client.delete("/files/file_1")
    assert resp.status_code == 403


def test_admin_can_delete_a_file(client, db_engine, auth_as):
    ag_id = _setup(db_engine, Role.ADMIN)
    auth_as("0000-0001", groups=[("grp_1", ag_id, Role.ADMIN)])

    resp = client.delete("/files/file_1")
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        assert session.get(AssociatedFile, 1) is None


def test_editor_can_still_patch_a_file(client, db_engine, auth_as):
    """PATCH (metadata update) stays at the editor threshold -- only delete
    moved to admin."""
    ag_id = _setup(db_engine, Role.EDITOR)
    auth_as("0000-0001", groups=[("grp_1", ag_id, Role.EDITOR)])

    resp = client.patch("/files/file_1", json={"access_note": "updated"})
    assert resp.status_code == 200, resp.text
