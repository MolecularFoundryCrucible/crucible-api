import pytest
from sqlmodel import Session

from app.tables import AccessGroup, AssociatedFile, Dataset, DeletionRequest, Resource, ResourceACL, User
from app.utils.permissions import PlatformRole, Role

DS = "0t3qajrnzhtn7000x6s0x427hg"
ME = "0000-0002-1825-0097"


def _dataset_with_file(db_engine, pending=False):
    with Session(db_engine) as session:
        session.add(User(first_name="A", last_name="B", unique_id=ME))
        session.add(Resource(mfid=DS, resource_type="dataset"))
        dataset = Dataset(unique_id=DS, dataset_name="d", owner_orcid="0000-0000-0000-0001")
        group = AccessGroup(group_name="readers")
        session.add_all([dataset, group])
        session.commit()
        session.add(ResourceACL(mfid=DS, accessgroup_id=group.id, read=True, write=True, permission="owner"))
        file = AssociatedFile(dataset_id=dataset.id, filename="a.txt", size=10, sha256_hash="a" * 64, storage_path=f"mf-storage-prod/{DS}/a.txt")
        session.add(file)
        if pending:
            session.add(DeletionRequest(resource_id=DS, resource_type="dataset", requester_id=ME, status="pending"))
        session.commit()
        return group.id, file.mfid


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR, Role.EDITOR, Role.ADMIN, Role.OWNER])
def test_the_capability_says_whether_deleting_a_file_would_be_accepted(client, db_engine, auth_as, role):
    group_id, file_mfid = _dataset_with_file(db_engine)
    auth_as(ME, groups=[("readers", group_id, role)])
    capable = client.get(f"/datasets/{DS}").json()["capabilities"]["can_delete_files"]
    assert client.get(f"/resources/{DS}").json()["capabilities"]["can_delete_files"] is capable
    assert capable is (role >= Role.ADMIN)
    assert client.delete(f"/files/{file_mfid}").status_code == (200 if capable else 403)


def test_no_file_can_be_deleted_from_a_dataset_pending_deletion(client, db_engine, auth_as):
    _, file_mfid = _dataset_with_file(db_engine, pending=True)
    auth_as(ME, platform_role=PlatformRole.ADMIN)
    assert client.get(f"/datasets/{DS}").json()["capabilities"]["can_delete_files"] is False
    assert client.delete(f"/files/{file_mfid}").status_code == 409

