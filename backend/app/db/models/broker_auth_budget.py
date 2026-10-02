import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONColumn


class BrokerAuthBudget(Base):
    __tablename__ = "broker_auth_budgets"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSONColumn, nullable=False)
