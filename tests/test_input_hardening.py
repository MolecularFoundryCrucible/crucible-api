import pytest
from sqlmodel import Session, SQLModel

from app.general_utils import is_orcid, is_valid_mfid
from app.tables import Keyword, User
from app.utils.permissions import PlatformRole

ME = "0000-0002-1825-0097"


@pytest.mark.parametrize("check, value", [(is_orcid, ME), (is_valid_mfid, "0tmnyhfztdwc1000w2kspzsnh0")])
def test_identifier_checks_refuse_a_trailing_newline(check, value):
    assert check(value) and not check(value + "\n") and not check("x" + value)


@pytest.mark.parametrize("path", ["/keywords", "/keywords/xrd"])
def test_keywords_need_sign_in(client, db_engine, auth_as, path):
    SQLModel.metadata.create_all(db_engine, tables=[Keyword.__table__])
    assert client.get(path).status_code in (401, 403)
    auth_as(ME)
    assert client.get(path).status_code == 200


def test_user_search_treats_wildcards_as_text(client, db_engine, auth_as):
    with Session(db_engine) as session, session.begin():
        session.add(User(first_name="Ada", last_name="Lovelace", username="ada", unique_id=ME))
        session.add(User(first_name="100%", last_name="Under_score", username="percent", unique_id="0000-0001-5109-3700"))
    auth_as(ME, platform_role=PlatformRole.ADMIN)

    def found(**params):
        answer = client.get("/users", params=params)
        assert answer.status_code == 200, answer.text
        return sorted(item["username"] for item in answer.json()["items"])

    assert found(q="%") == ["percent"] and found(q="_") == ["percent"]
    assert found(q="LOVE") == ["ada"]
    assert found(first_name="%") == ["percent"] and found(last_name="r_s") == ["percent"] and found(first_name="ad") == ["ada"]


@pytest.mark.parametrize("path, body", [
    ("/users/resolve", {"orcids": [ME] * 1001}),
    ("/users/resolve", {"usernames": ["ada"] * 1001}),
    ("/users/resolve", {"emails": ["ada@example.org"] * 1001}),
    ("/datasets/first_thumbnails", ["0tmnyhfztdwc1000w2kspzsnh1"] * 1001),
])
def test_lookup_lists_are_capped(client, auth_as, path, body):
    auth_as(ME)
    assert client.post(path, json=body).status_code == 422
    shorter = {key: value[:1000] for key, value in body.items()} if isinstance(body, dict) else body[:1000]
    assert client.post(path, json=shorter).status_code == 200
