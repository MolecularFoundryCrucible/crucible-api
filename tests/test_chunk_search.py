from sqlalchemy.dialects import postgresql

from app.api.routes.resources import search_chunks
from app.models.ai import ChunkHit, ChunkSearchRequest
from app.tables import ResourceEmbedding
from app.utils.permissions import GroupMembership, Principal, Role


QUERY_VECTOR = [0.0] * 768


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]


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


class _EmbeddingListSession:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def exec(self, statement):
        self.calls.append(statement)
        if len(self.calls) == 1:
            return _Result([1])
        return _Result([
            ResourceEmbedding(
                id=7,
                resource_mfid="resource-1",
                chunk_index=2,
                chunk_text="matching text",
                embedding=QUERY_VECTOR,
                model_name="model-a",
            )
        ])


class _EmbeddingCursorSession:
    def __init__(self, rows, total=None):
        self.rows = rows
        self.total = total
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def exec(self, statement):
        self.calls.append(statement)
        if self.total is not None and len(self.calls) == 1:
            return _Result([self.total])
        return _Result(self.rows)


def _embedding(embedding_id):
    return ResourceEmbedding(
        id=embedding_id,
        resource_mfid="resource-1",
        chunk_index=embedding_id - 1,
        chunk_text=f"chunk {embedding_id}",
        embedding=QUERY_VECTOR,
        model_name="model-a",
    )


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


def test_list_resource_embeddings_is_paginated_filtered_and_authorized(
    client, auth_as, monkeypatch
):
    from app.api.routes import resources

    auth_as("user-1")
    session = _EmbeddingListSession()
    monkeypatch.setattr(resources, "Session", lambda engine: session)

    response = client.get(
        "/embeddings",
        params={
            "id": 7,
            "resource_mfid": "resource-1",
            "chunk_index": 2,
            "model_name": "model-a",
            "limit": 5,
            "offset": 10,
            "include_total": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 5
    assert payload["offset"] == 10
    assert payload["next_cursor"] is None
    assert payload["items"][0] == {
        "id": 7,
        "resource_mfid": "resource-1",
        "chunk_index": 2,
        "chunk_text": "matching text",
        "embedding": QUERY_VECTOR,
        "model_name": "model-a",
    }

    count_statement = str(session.calls[0].compile(dialect=postgresql.dialect()))
    page_statement = str(session.calls[1].compile(dialect=postgresql.dialect()))
    assert "resourceacl.mfid = resource_embedding.resource_mfid" in count_statement
    assert "resource_embedding.id =" in count_statement
    assert "resource_embedding.resource_mfid =" in count_statement
    assert "resource_embedding.chunk_index =" in count_statement
    assert "resource_embedding.model_name =" in count_statement
    assert "ORDER BY resource_embedding.id" in page_statement
    assert session.calls[1]._limit_clause.value == 6
    assert session.calls[1]._offset_clause.value == 10


def test_list_resource_embeddings_follows_signed_cursor_pages(
    client, auth_as, monkeypatch
):
    from app.api.routes import resources

    auth_as("user-1")
    first_session = _EmbeddingCursorSession(
        [_embedding(embedding_id) for embedding_id in range(1, 7)],
        total=7,
    )
    second_session = _EmbeddingCursorSession(
        [_embedding(embedding_id) for embedding_id in range(6, 8)],
    )
    sessions = iter([first_session, second_session])
    monkeypatch.setattr(resources, "Session", lambda engine: next(sessions))

    first_response = client.get("/embeddings", params={"limit": 5})

    assert first_response.status_code == 200
    first_page = first_response.json()
    assert first_page["total"] == 7
    assert first_page["offset"] == 0
    assert [item["id"] for item in first_page["items"]] == [1, 2, 3, 4, 5]
    assert first_page["next_cursor"]

    second_response = client.get(
        "/embeddings",
        params={
            "limit": 5,
            "cursor": first_page["next_cursor"],
            "offset": 999,
        },
    )

    assert second_response.status_code == 200
    second_page = second_response.json()
    assert second_page["total"] is None
    assert second_page["offset"] is None
    assert [item["id"] for item in second_page["items"]] == [6, 7]
    assert second_page["next_cursor"] is None

    page_statement = str(
        second_session.calls[0].compile(dialect=postgresql.dialect())
    )
    assert "resource_embedding.id >" in page_statement
    assert second_session.calls[0]._offset_clause is None
    assert second_session.calls[0]._limit_clause.value == 6


def test_list_resource_embeddings_cursor_is_bound_to_filters(
    client, auth_as, monkeypatch
):
    from app.api.routes import resources

    auth_as("user-1")
    session = _EmbeddingCursorSession(
        [_embedding(embedding_id) for embedding_id in range(1, 3)],
        total=2,
    )
    monkeypatch.setattr(resources, "Session", lambda engine: session)
    first_response = client.get("/embeddings", params={"limit": 1})
    cursor = first_response.json()["next_cursor"]

    response = client.get(
        "/embeddings",
        params={"limit": 1, "cursor": cursor, "model_name": "model-b"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid cursor"


def test_list_resource_embeddings_rejects_unknown_filters(client, auth_as):
    auth_as("user-1")

    response = client.get("/embeddings", params={"unknown": "value"})

    assert response.status_code == 422


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
        "/embeddings/search",
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
