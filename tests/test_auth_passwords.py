import statistics
import time

import pytest
from sqlmodel import Session, SQLModel, select

from app import config
from app.auth import passwords
from app.auth.passwords import ServiceAccountPassword, check_new_password, hash_password, store_password, verify_dummy, verify_password
from app.config import ConfigurationError, check_settings, get_settings
from app.tables import LocalCredential, User

PEPPER = "p" * 40
GOOD = "violet ladder under quiet rain"
UNCOMMON = "qvexmilorthapunzik walrus"


@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    for name, value in {"CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "CRUCIBLE_AUTH_PEPPER": PEPPER, "CRUCIBLE_AUTH_PEPPER_ID": "one"}.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()


def settings(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setenv(f"CRUCIBLE_AUTH_{name.upper()}", str(value))
    config.get_settings.cache_clear()


@pytest.mark.parametrize("memory,iterations", [(19455, 2), (19456, 1), (47103, 1), (7168, 4)])
def test_parameters_below_floor_fail_settings_check(monkeypatch, memory, iterations):
    settings(monkeypatch, argon2_memory_kib=memory, argon2_time_cost=iterations)
    with pytest.raises(ConfigurationError, match="OWASP"):
        get_settings()


@pytest.mark.parametrize("memory,iterations", [(19456, 2), (47104, 1), (7168, 5), (65536, 3)])
def test_parameters_at_or_above_floor_pass(monkeypatch, memory, iterations):
    settings(monkeypatch, argon2_memory_kib=memory, argon2_time_cost=iterations)
    assert get_settings().auth.argon2_memory_kib == memory


def test_default_parameters_are_above_floor(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_AUTH_ARGON2_MEMORY_KIB")
    monkeypatch.delenv("CRUCIBLE_AUTH_ARGON2_TIME_COST")
    config.get_settings.cache_clear()
    auth = get_settings().auth
    assert (auth.argon2_memory_kib, auth.argon2_time_cost, auth.argon2_parallelism, auth.password_min_length) == (65536, 3, 1, 15)


def test_hash_is_argon2id_phc_string_and_two_hashes_differ():
    first, pepper_id = hash_password(GOOD)
    second, _ = hash_password(GOOD)
    assert first.startswith("$argon2id$v=19$m=19456,t=2,p=1$") and pepper_id == "one"
    assert first != second
    assert GOOD not in first and PEPPER not in first


def test_composed_and_decomposed_forms_verify():
    stored, pepper_id = hash_password("café au lait on the terrace")
    assert verify_password("café au lait on the terrace", stored, pepper_id)[0]


def test_spaces_preserved():
    stored, pepper_id = hash_password("  pass phrase here long  ")
    assert verify_password("  pass phrase here long  ", stored, pepper_id)[0]
    assert not verify_password("pass phrase here long", stored, pepper_id)[0]
    assert not verify_password("passphraseherelong", stored, pepper_id)[0]


@pytest.mark.parametrize("password,stored", [("", "$argon2id$v=19$m=19456,t=2,p=1$abc$def"), (GOOD, None), (GOOD, ""), (GOOD, "not-a-hash")])
def test_empty_password_or_missing_hash_never_verifies(password, stored):
    assert verify_password(password, stored, "one") == (False, None)


def test_wrong_pepper_fails_and_unknown_pepper_id_fails():
    stored, _ = hash_password(GOOD)
    assert not verify_password(GOOD, stored, None)[0]
    assert not verify_password(GOOD, stored, "never-configured")[0]


def test_rehash_returned_when_parameters_raised(monkeypatch):
    stored, pepper_id = hash_password(GOOD)
    settings(monkeypatch, argon2_memory_kib=47104, argon2_time_cost=2)
    valid, upgraded = verify_password(GOOD, stored, pepper_id)
    assert valid and upgraded[0].startswith("$argon2id$v=19$m=47104,t=2,p=1$") and upgraded[1] == "one"
    assert verify_password(GOOD, *upgraded) == (True, None)


def test_old_pepper_verifies_during_rotation_and_is_replaced(monkeypatch):
    stored, pepper_id = hash_password(GOOD)
    settings(monkeypatch, pepper="q" * 40, pepper_id="two", previous_pepper_ids="one", previous_pepper_one=PEPPER)
    valid, upgraded = verify_password(GOOD, stored, pepper_id)
    assert valid and upgraded[1] == "two"
    monkeypatch.delenv("CRUCIBLE_AUTH_PREVIOUS_PEPPER_IDS")
    config.get_settings.cache_clear()
    assert not verify_password(GOOD, stored, pepper_id)[0]
    assert verify_password(GOOD, *upgraded) == (True, None)


def test_missing_or_empty_or_short_pepper_refuses_startup_when_passwords_enabled(monkeypatch, orcid_provider_settings):
    settings(monkeypatch, password_enabled="true")
    check_settings()
    for value in ("", " "):
        settings(monkeypatch, pepper=value)
        with pytest.raises(ConfigurationError, match="CRUCIBLE_AUTH_PEPPER"):
            check_settings()
    monkeypatch.delenv("CRUCIBLE_AUTH_PEPPER")
    config.get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="CRUCIBLE_AUTH_PEPPER"):
        check_settings()
    settings(monkeypatch, pepper="short")
    with pytest.raises(ConfigurationError, match="at least 32 characters"):
        config.get_secret("CRUCIBLE_AUTH_PEPPER")


def test_a_listed_previous_pepper_must_be_given(monkeypatch):
    settings(monkeypatch, password_enabled="true", previous_pepper_ids="old")
    with pytest.raises(ConfigurationError, match="CRUCIBLE_AUTH_PREVIOUS_PEPPER_OLD"):
        check_settings()


def test_verify_dummy_takes_comparable_time():
    stored, pepper_id = hash_password(GOOD)

    def median(call):
        runs = []
        for _ in range(7):
            start = time.perf_counter()
            call()
            runs.append(time.perf_counter() - start)
        return statistics.median(runs)

    real = median(lambda: verify_password("wrong password, long enough", stored, pepper_id))
    dummy = median(lambda: verify_dummy("wrong password, long enough"))
    missing = median(lambda: verify_password("wrong password, long enough", None, None))
    assert 0.5 < dummy / real < 2 and 0.5 < missing / real < 2


def test_15_code_points_accepted_14_refused_by_default():
    assert check_new_password(UNCOMMON[:14], context=[]).code == "too_short"
    assert check_new_password(UNCOMMON[:15], context=[]) is None


def test_minimum_can_be_lowered_to_12_and_no_further(monkeypatch):
    settings(monkeypatch, password_min_length=12)
    assert check_new_password(UNCOMMON[:12], context=[]) is None
    assert check_new_password(UNCOMMON[:11], context=[]).code == "too_short"
    settings(monkeypatch, password_min_length=11)
    with pytest.raises(ConfigurationError):
        get_settings()


def test_emoji_count_as_one_each():
    assert check_new_password("\U0001f600" * 14, context=[]).code == "too_short"
    assert check_new_password("\U0001f600" * 15, context=[]) is None


def test_256_accepted_257_refused():
    assert check_new_password("x" * 255 + "y", context=[]) is None
    assert check_new_password("x" * 256 + "y", context=[]).code == "too_long"


def test_no_composition_rule():
    assert check_new_password("all lowercase words without digits", context=[]) is None


def test_blocklist_whole_match_case_insensitive_with_reason():
    problem = check_new_password("Correct Horse Battery Staple", context=[])
    assert problem.code == "common" and "common" in problem.message
    assert check_new_password("correct horse battery staple in the attic", context=[]) is None


def test_blocklist_is_loaded_from_the_shipped_file():
    assert len(passwords.blocked_passwords()) > 20000
    assert all(len(word) >= 12 for word in passwords.blocked_passwords())


CONTEXT = ["ada.lovelace.engine", "ada.lovelace@example.org", "Augusta Ada King-Noel", None]


@pytest.mark.parametrize("password", ["ada.lovelace.engine", "ADA.LOVELACE@EXAMPLE.ORG", "augusta ada king-noel"])
def test_context_username_email_names_refused(password):
    assert check_new_password(password, context=CONTEXT).code == "personal"
    assert check_new_password(password + " plus more words", context=CONTEXT) is None


def test_service_name_refused_once_long_enough(monkeypatch):
    settings(monkeypatch, password_min_length=12)
    monkeypatch.setattr(passwords, "SERVICE_NAME", "crucible-service")
    assert check_new_password("Crucible-Service", context=[]).code == "personal"


def test_store_password_writes_one_row_and_refuses_service_accounts(db_engine):
    SQLModel.metadata.create_all(db_engine, tables=[LocalCredential.__table__])
    with Session(db_engine) as session:
        person = User(unique_id="0tmnyhfztdwc1000w2kspzsnh0", first_name="A", last_name="B")
        robot = User(unique_id="0tmnyhfztdwc1000w2kspzsnh1", first_name="R", last_name="B", is_service_account=True)
        session.add_all([person, robot])
        session.commit()
        store_password(session, person, GOOD)
        store_password(session, person, GOOD + " again")
        session.commit()
        with pytest.raises(ServiceAccountPassword):
            store_password(session, robot, GOOD)
        rows = session.exec(select(LocalCredential)).all()
        assert [(row.user_id, row.pepper_id) for row in rows] == [(person.id, "one")]
        assert verify_password(GOOD + " again", rows[0].password_hash, rows[0].pepper_id)[0]


def test_hash_not_in_user_read_or_any_response_schema():
    from app.api.main import app

    schemas = app.openapi()["components"]["schemas"]
    assert not [name for name, schema in schemas.items() if {"password_hash", "pepper_id"} & set(schema.get("properties", {}))]


@pytest.mark.parametrize("way", ["variable", "file", "secret manager"])
def test_the_pepper_is_used_however_it_is_given(monkeypatch, tmp_path, way):
    monkeypatch.delenv("CRUCIBLE_AUTH_PEPPER")
    if way == "variable":
        monkeypatch.setenv("CRUCIBLE_AUTH_PEPPER", PEPPER)
    elif way == "file":
        (tmp_path / "pepper").write_text(PEPPER)
        monkeypatch.setenv("CRUCIBLE_AUTH_PEPPER_FILE", str(tmp_path / "pepper"))
    else:
        version = "projects/p/secrets/pepper/versions/1"
        monkeypatch.setenv("CRUCIBLE_AUTH_PEPPER_VERSION", version)
        monkeypatch.setattr(config, "get_gcs_secret", lambda name: PEPPER if name == version else "")
        monkeypatch.setattr(config, "_fetched_secrets", {})
    config.get_settings.cache_clear()
    assert passwords._current_pepper() == ("one", PEPPER.encode())
    stored, pepper_id = hash_password(GOOD)
    assert pepper_id == "one" and verify_password(GOOD, stored, pepper_id)[0]


def test_a_pepper_identifier_without_its_secret_never_hashes_unpeppered(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_AUTH_PEPPER")
    config.get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="CRUCIBLE_AUTH_PEPPER"):
        hash_password(GOOD)


def test_secret_settings_are_read_only_through_the_secret_helpers():
    import pathlib
    import re

    root = pathlib.Path(config.__file__).parent
    readers = [str(path.relative_to(root)) for path in root.rglob("*.py")
               if path.name != "config.py" and re.search(r"\.get_secret_value\(\)", path.read_text())]
    assert readers == [], "read secrets with get_secret or optional_secret, which also handle NAME_FILE and NAME_VERSION"
