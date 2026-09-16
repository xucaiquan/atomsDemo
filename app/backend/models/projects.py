from core.database import Base
from datetime import datetime as PyDateTime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column


class Projects(Base):
    __tablename__ = "projects"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True, nullable=False)
    public_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    version_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latest_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_demo: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[Optional[PyDateTime]] = mapped_column(DateTime(timezone=True), default=PyDateTime.now)
    updated_at: Mapped[Optional[PyDateTime]] = mapped_column(DateTime(timezone=True), default=PyDateTime.now, onupdate=PyDateTime.now)