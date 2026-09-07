from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import func, literal, or_, text


MAX_SEARCH_QUERY_LENGTH = 200
MAX_SEARCH_QUERY_TOKENS = 10
SEARCH_QUERY_LIMIT_DESCRIPTION = (
    f"Queries may contain at most {MAX_SEARCH_QUERY_LENGTH} characters and "
    f"{MAX_SEARCH_QUERY_TOKENS} terms."
)


def normalize_search_query(query: str) -> tuple[str, list[str]]:
    normalized = " ".join(query.lower().split())
    if len(normalized) < 3:
        raise HTTPException(
            status_code=422,
            detail="Search query must contain at least 3 non-whitespace characters",
        )
    if len(normalized) > MAX_SEARCH_QUERY_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Search query may contain at most {MAX_SEARCH_QUERY_LENGTH} characters",
        )
    tokens = normalized.split()
    if len(tokens) > MAX_SEARCH_QUERY_TOKENS:
        raise HTTPException(
            status_code=422,
            detail=f"Search query may contain at most {MAX_SEARCH_QUERY_TOKENS} terms",
        )
    return normalized, tokens


def configure_trigram_search(session, threshold: float) -> None:
    if not 0 <= threshold <= 1:
        raise ValueError("trgm_search_threshold must be between 0 and 1")
    session.execute(
        text("SELECT set_config('pg_trgm.word_similarity_threshold', :threshold, true)"),
        {"threshold": str(threshold)},
    )


def apply_trigram_search(query, tokens: Sequence[str], fields, prefix_fields=()):
    for token in tokens:
        predicates = [field.istartswith(token, autoescape=True) for field in prefix_fields]
        predicates.extend(literal(token).op("<%")(field) for field in fields)
        query = query.where(or_(*predicates))
    return query


def trigram_rank(query: str, fields):
    scores = [func.coalesce(func.word_similarity(query, field), 0) for field in fields]
    return scores[0] if len(scores) == 1 else func.greatest(*scores)
