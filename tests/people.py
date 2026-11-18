from sqlmodel import Session, select

from app.tables import User


def internal_id(bind, unique_id: str) -> int:
    """`user.id` of the person with this `unique_id`, through an engine or an open session."""
    if isinstance(bind, Session):
        return bind.exec(select(User.id).where(User.unique_id == unique_id)).one()
    with Session(bind) as session:
        return internal_id(session, unique_id)


def person_row(session: Session, unique_id: str) -> User:
    return session.exec(select(User).where(User.unique_id == unique_id)).one()


def person_principal(user_id: str, **fields):
    """A Principal for a person with a user row, created if missing, in the database the routes use."""
    from app.api.dependencies import get_engine
    from app.utils.permissions import Principal

    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            user = User(unique_id=user_id, first_name="", last_name="", email=None)
            session.add(user)
            session.commit()
            session.refresh(user)
        return Principal(user_id=user_id, internal_user_id=user.id, **fields)


def user_id_or_none(session: Session, unique_id) -> int | None:
    """`user.id` for a stored person reference, or None when it matches nobody, as the migration fills it."""
    if unique_id is None:
        return None
    return session.exec(select(User.id).where(User.unique_id == unique_id)).first()


def add_old_style_key(session: Session, user_id, token: str, token_created: str, **fields):
    """A key from before 4.0, as the migration copied it into api_key."""
    from datetime import date, datetime, time, timezone

    from app.auth.api_keys import store_key

    row = store_key(session, user_id, token, created_at=datetime.combine(date.fromisoformat(token_created[:10]), time.min, tzinfo=timezone.utc), name="Default key")
    for name, value in fields.items():
        setattr(row, name, value)
    return row
