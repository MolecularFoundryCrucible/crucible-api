from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.tables import Instrument, ScientificMetadataBinary, User


def _ddl(table, name):
    index = next(index for index in table.indexes if index.name == name)
    return str(CreateIndex(index).compile(dialect=postgresql.dialect())).strip()


def test_models_declare_the_indexes_production_has():
    assert _ddl(User.__table__, "ix_user_username") == 'CREATE UNIQUE INDEX ix_user_username ON "user" (username) WHERE username IS NOT NULL'
    assert _ddl(Instrument.__table__, "ix_instrument_instrument_id") == "CREATE UNIQUE INDEX ix_instrument_instrument_id ON instrument (instrument_id) WHERE instrument_id IS NOT NULL"
    assert _ddl(ScientificMetadataBinary.__table__, "ix_scientific_metadata_new_unique_id") == "CREATE INDEX ix_scientific_metadata_new_unique_id ON scientific_metadata_new (unique_id)"
    assert any(constraint.name == "scientific_metadata_new_unique_id_key" and [c.name for c in constraint.columns] == ["unique_id"]
               for constraint in ScientificMetadataBinary.__table__.constraints)
