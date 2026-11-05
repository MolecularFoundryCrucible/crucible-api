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
