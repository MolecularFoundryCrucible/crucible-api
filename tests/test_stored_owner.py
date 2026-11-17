from app.tables import Dataset, Instrument, Sample, User
from app.utils.gcs_record_utils import instrument_record
from app.utils.user_utils import owner_reference


def test_stored_records_keep_an_owner_no_user_matches():
    person = User(id=7, unique_id="0000-0001-0000-0001", first_name="A", last_name="B")
    assert owner_reference(Dataset(unique_id="d", owner_orcid="0000-0009-0000-0009"), None) == "0000-0009-0000-0009"
    assert owner_reference(Sample(unique_id="s", owner_orcid="0000-0009-0000-0009"), None) == "0000-0009-0000-0009"
    assert owner_reference(Dataset(unique_id="d", owner_orcid="stale", owner_user_id=7), person) == person.unique_id
    assert owner_reference(Dataset(unique_id="d", owner_orcid="stale", owner_user_id=8), None) is None
    instrument = Instrument(unique_id="i", instrument_id="i", instrument_name="i", location="b30", owner="0000-0009-0000-0009")
    assert instrument_record(instrument, None, None)["owner"] == "0000-0009-0000-0009"
