import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from app.tables import Dataset
from app.utils.search_utils import (
    apply_trigram_search,
    configure_trigram_search,
    normalize_search_query,
    trigram_rank,
)


def test_normalize_search_query_collapses_whitespace_and_case():
    assert normalize_search_query("  Mixed\tCASE  terms ") == (
        "mixed case terms",
        ["mixed", "case", "terms"],
    )


@pytest.mark.parametrize("query", ["   ", " a "])
def test_normalize_search_query_rejects_short_normalized_input(query):
    with pytest.raises(HTTPException) as error:
        normalize_search_query(query)

    assert error.value.status_code == 422


def test_normalize_search_query_rejects_more_than_ten_terms():
    with pytest.raises(HTTPException) as error:
        normalize_search_query(" ".join(f"term{i}" for i in range(11)))

    assert error.value.status_code == 422
    assert error.value.detail == "Search query may contain at most 10 terms"


def test_normalize_search_query_rejects_more_than_two_hundred_characters():
    with pytest.raises(HTTPException) as error:
        normalize_search_query("x" * 201)

    assert error.value.status_code == 422
    assert error.value.detail == "Search query may contain at most 200 characters"


def test_trigram_search_escapes_prefix_wildcards_and_uses_stable_tie_breaker():
    statement = apply_trigram_search(
        select(Dataset),
        ["100%_"],
        fields=(Dataset.dataset_name,),
        prefix_fields=(Dataset.dataset_name,),
    ).order_by(
        trigram_rank("100%_", (Dataset.dataset_name,)).desc(),
        Dataset.unique_id.desc(),
    )
    compiled = str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))

    assert "ESCAPE '/'" in compiled
    assert "100/%%/_" in compiled
    assert "word_similarity" in compiled
    assert "dataset.unique_id DESC" in compiled


def test_configure_trigram_search_binds_the_threshold():
    calls = []

    class SessionStub:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))

    configure_trigram_search(SessionStub(), 0.3)

    assert calls == [(
        "SELECT set_config('pg_trgm.word_similarity_threshold', :threshold, true)",
        {"threshold": "0.3"},
    )]


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_configure_trigram_search_rejects_invalid_threshold(threshold):
    with pytest.raises(ValueError):
        configure_trigram_search(None, threshold)
