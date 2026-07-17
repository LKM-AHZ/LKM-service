from app.db.base import Base
from app.db.session import engine
from app.modules.auth.models import User


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
