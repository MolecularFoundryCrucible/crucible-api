from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.config import get_settings
from app.models.core_models import (
    AuthEvent, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily,
)


def cleanup_hub_auth(engine, *, commit: bool = False, batch_size: int = 1000, now: datetime | None = None) -> dict[str, int]:
    if not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    cutoff = now or datetime.now(timezone.utc)
    counts = {}
    expired_access_ids = []
    with Session(engine) as session, session.begin():
        for model, key in (
            (HubLoginTransaction, HubLoginTransaction.state_hash),
            (HubLoginCode, HubLoginCode.code_hash),
            (HubAccessToken, HubAccessToken.token_hash),
        ):
            query = select(key).where(model.expires_at <= cutoff).order_by(model.expires_at, key).limit(batch_size)
            if commit:
                query = query.with_for_update(skip_locked=True)
            ids = session.exec(query).all()
            if model is HubAccessToken:
                expired_access_ids = ids
            counts[model.__tablename__] = len(ids)
            if commit and ids:
                session.execute(delete(model).where(key.in_(ids)))

        query = select(HubTokenFamily.id).where(HubTokenFamily.expires_at <= cutoff).order_by(HubTokenFamily.expires_at, HubTokenFamily.id).limit(batch_size)
        if commit:
            query = query.with_for_update(skip_locked=True)
        families = session.exec(query).all()
        for model in (HubAccessToken, HubRefreshToken):
            condition = model.family_id.in_(families)
            if model is HubAccessToken and not commit:
                condition = condition & model.token_hash.not_in(expired_access_ids)
            counts[model.__tablename__] = counts.get(model.__tablename__, 0) + session.exec(select(func.count()).select_from(model).where(condition)).one()
            if commit and families:
                session.execute(delete(model).where(model.family_id.in_(families)))
        counts[HubTokenFamily.__tablename__] = len(families)
        if commit and families:
            session.execute(delete(HubTokenFamily).where(HubTokenFamily.id.in_(families)))

        oldest_kept = cutoff - timedelta(days=get_settings().auth.event_retention_days)
        query = select(AuthEvent.id).where(AuthEvent.at < oldest_kept).order_by(AuthEvent.at, AuthEvent.id).limit(batch_size)
        if commit:
            query = query.with_for_update(skip_locked=True)
        events = session.exec(query).all()
        counts[AuthEvent.__tablename__] = len(events)
        if commit and events:
            session.execute(delete(AuthEvent).where(AuthEvent.id.in_(events)))
    from app.auth.throttle import delete_address_rows, expired_address_rows

    from app.auth.devices import remove_expired

    with Session(engine) as session, session.begin():
        counts["device_authorization"] = remove_expired(session, cutoff, batch_size, commit=commit)
    with Session(engine) as session, session.begin():
        digests = expired_address_rows(session, cutoff, batch_size, lock=commit)
        counts["login_throttle"] = len(digests)
        if commit:
            delete_address_rows(session, digests)
    from app.auth.registration import expire_pending_users

    counts["pending_users"] = expire_pending_users(engine, commit=commit, batch_size=batch_size, now=now)
    return counts
