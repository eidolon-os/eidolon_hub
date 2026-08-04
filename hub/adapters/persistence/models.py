"""Relational schema owned by the local Eidolon Hub."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DeviceRow(Base):
    __tablename__ = "hub_devices"

    device_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    enrollment_id: Mapped[str] = mapped_column(String(255))
    retrieval_token_hash: Mapped[str] = mapped_column(String(64))
    retrieval_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    display_name: Mapped[str] = mapped_column(String(512))
    device_kind: Mapped[str] = mapped_column(String(255), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    manifest_revision: Mapped[str] = mapped_column(String(80))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_enrollment_request_id: Mapped[str] = mapped_column(String(255), default="")
    last_enrollment_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    owner_id: Mapped[str | None] = mapped_column(String(255), index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32), index=True)
    last_management_request_id: Mapped[str] = mapped_column(String(255), default="")
    last_management_fingerprint: Mapped[str] = mapped_column(String(128), default="")

    __table_args__ = (Index("ix_hub_devices_enrollment", "enrollment_id", unique=True),)


class DeviceManagementEventRow(Base):
    __tablename__ = "hub_events"

    stream_position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(512), index=True)
    owner_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_json: Mapped[str] = mapped_column(Text)
