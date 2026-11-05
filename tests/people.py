from sqlmodel import Session, select

from app.models.core_models import User


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
