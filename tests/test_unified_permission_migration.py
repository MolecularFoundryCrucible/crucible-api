import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REVISION = ROOT / "migrations/versions/20260813_ecfa70615e54_rename_resourceacl_role_to_permission.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("permission_rename_revision", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Inspector:
    def __init__(self, columns, indexes):
        self.columns = columns
        self.indexes = indexes

    def get_columns(self, table):
        assert table == "resourceacl"
        return [{"name": name} for name in self.columns]

    def get_indexes(self, table):
        assert table == "resourceacl"
        return [{"name": name} for name in self.indexes]


def test_rename_revision_is_noop_for_unified_production_schema(monkeypatch):
    revision = _load_revision()
    calls = []
    monkeypatch.setattr(revision.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        revision.sa,
        "inspect",
        lambda bind: _Inspector(
            {"mfid", "permission"},
            {"ix_resourceacl_group_mfid_permission"},
        ),
    )
    monkeypatch.setattr(revision.op, "alter_column", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(revision.op, "execute", lambda statement: calls.append(statement))

    revision.upgrade()

    assert calls == []


def test_rename_revision_preserves_staging_compatibility(monkeypatch):
    revision = _load_revision()
    calls = []
    monkeypatch.setattr(revision.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        revision.sa,
        "inspect",
        lambda bind: _Inspector(
            {"mfid", "role"},
            {"ix_resourceacl_group_mfid_role"},
        ),
    )
    monkeypatch.setattr(revision.op, "alter_column", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(revision.op, "execute", lambda statement: calls.append(statement))

    revision.upgrade()

    assert calls == [
        (("resourceacl", "role"), {"new_column_name": "permission"}),
        "ALTER INDEX ix_resourceacl_group_mfid_role RENAME TO ix_resourceacl_group_mfid_permission",
    ]
