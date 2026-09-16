from sqlalchemy import func, literal, or_
from sqlmodel import Session

import app.api.routes.users as users_routes
from app.models.core_models import User
from app.utils.permissions import Principal


def _seed_users(db_engine):
    with Session(db_engine) as session:
        session.add_all([
            User(
                first_name="Beamline",
                last_name="Uploader",
                email=None,
                unique_id="aa-service-account-search",
                username="picker-service",
                is_service_account=True,
            ),
            User(
                first_name="Human",
                last_name="Uploader",
                email="human@example.com",
                unique_id="zz-human-search",
                username="picker-human",
                is_service_account=False,
            ),
        ])
        session.commit()


def _configure_sqlite_search(monkeypatch):
    monkeypatch.setattr(
        users_routes,
        "configure_trigram_search",
        lambda session, threshold: None,
    )

    def apply_search(query, tokens, fields, prefix_fields=()):
        for token in tokens:
            query = query.where(
                or_(*(func.lower(field).contains(token) for field in fields))
            )
        return query

    monkeypatch.setattr(users_routes, "apply_trigram_search", apply_search)
    monkeypatch.setattr(users_routes, "trigram_rank", lambda query, fields: literal(1))


def test_user_search_filters_service_accounts_before_limiting(db_engine, monkeypatch):
    _seed_users(db_engine)
    _configure_sqlite_search(monkeypatch)

    results = users_routes.search_users(
        Principal(user_id="caller"),
        q="picker",
        is_service_account=True,
        limit=1,
    )

    assert [user.unique_id for user in results] == ["aa-service-account-search"]
    assert results[0].is_service_account is True


def test_user_search_can_filter_humans_or_return_both(db_engine, monkeypatch):
    _seed_users(db_engine)
    _configure_sqlite_search(monkeypatch)
    principal = Principal(user_id="caller")

    humans = users_routes.search_users(
        principal,
        q="picker",
        is_service_account=False,
        limit=20,
    )
    all_users = users_routes.search_users(
        principal,
        q="picker",
        is_service_account=None,
        limit=20,
    )

    assert [user.unique_id for user in humans] == ["zz-human-search"]
    assert {user.unique_id for user in all_users} == {
        "aa-service-account-search",
        "zz-human-search",
    }
