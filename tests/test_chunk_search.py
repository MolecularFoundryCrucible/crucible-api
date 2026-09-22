from sqlalchemy.dialects import postgresql

from app.api.routes.resources import search_chunks
from app.models.ai import ChunkHit, ChunkSearchRequest
from app.models.core_models import ResourceEmbedding
from app.utils.permissions import GroupMembership, Principal, Role


QUERY_VECTOR = [0.0] * 768


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self):
        self.calls = []

    def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        if parameters is not None:
            return _Result([])
        return _Result([
            (
                ResourceEmbedding(
                    id=1,
                    resource_mfid="resource-1",
                    chunk_index=2,
                    chunk_text="matching text",
                    embedding=QUERY_VECTOR,
                    model_name="model-a",
                ),
                0.25,
            )
        ])


def test_chunk_search_is_bounded_and_authorization_filtered():
    session = _Session()
    principal = Principal(
        user_id="user-1",
        groups=(GroupMembership(7, "user-1", Role.OWNER),),
    )

    hits = search_chunks(
        session,
        principal,
        QUERY_VECTOR,
        k=5,
        probes=12,
        model_name="model-a",
        resource_mfids=["resource-1"],
    )

    setting, parameters = session.calls[0]
    assert str(setting) == "SELECT set_config('ivfflat.probes', :probes, true)"
    assert parameters == {"probes": "12"}

    statement = session.calls[1][0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "resource_embedding.embedding <=>" in compiled
    assert "resourceacl.mfid = resource_embedding.resource_mfid" in compiled
    assert "resource_embedding.model_name =" in compiled
    assert "resource_embedding.resource_mfid IN" in compiled
    assert statement._limit_clause.value == 5
    assert hits == [
        ChunkHit(
            resource_mfid="resource-1",
            chunk_index=2,
            chunk_text="matching text",
            distance=0.25,
        )
    ]
    assert hits[0].similarity == 0.75


def test_chunk_search_request_requires_exact_vector_dimensions():
    request = ChunkSearchRequest(query_vector=QUERY_VECTOR)
    assert len(request.query_vector) == 768

    for invalid in (QUERY_VECTOR[:-1], QUERY_VECTOR + [0.0]):
        try:
            ChunkSearchRequest(query_vector=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid vector dimensions were accepted")


def test_chunk_search_endpoint_returns_similarity(client, auth_as, monkeypatch):
    from app.api.routes import resources

    auth_as("user-1")
    monkeypatch.setattr(
        resources,
        "search_chunks",
        lambda *args, **kwargs: [
            ChunkHit(
                resource_mfid="resource-1",
                chunk_index=2,
                chunk_text="matching text",
                distance=0.25,
            )
        ],
    )

    response = client.post(
        "/resources/embeddings/search",
        json={"query_vector": QUERY_VECTOR},
    )

    assert response.status_code == 200
    assert response.json() == [{
        "resource_mfid": "resource-1",
        "chunk_index": 2,
        "chunk_text": "matching text",
        "distance": 0.25,
        "similarity": 0.75,
    }]
