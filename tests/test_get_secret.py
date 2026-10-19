import pytest

from app import general_utils


def test_missing_secret_without_a_store_name_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_TEST_UNSET_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CRUCIBLE_TEST_UNSET_SECRET is not set"):
        general_utils.get_secret("CRUCIBLE_TEST_UNSET_SECRET")


def test_environment_value_wins(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_SET_SECRET", "value")
    assert general_utils.get_secret("CRUCIBLE_TEST_SET_SECRET", "unused/versions/1") == "value"
