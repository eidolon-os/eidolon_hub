"""Relational schema owned by Eidolon Hub."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DeviceRow(Base):
    __tablename__ = "hub_devices"

    device_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    public_key_fingerprint: Mapped[str] = mapped_column(String(512))
    tenant_id: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str] = mapped_column(String(512))
    device_kind: Mapped[str] = mapped_column(String(255), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    manifest_revision: Mapped[str] = mapped_column(String(80))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_registration_request_id: Mapped[str] = mapped_column(String(255), default="")
    owner_id: Mapped[str | None] = mapped_column(String(255), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_management_request_id: Mapped[str] = mapped_column(String(255), default="")
    last_management_fingerprint: Mapped[str] = mapped_column(String(128), default="")


class CommandRow(Base):
    __tablename__ = "hub_commands"

    command_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(255), index=True)
    operation: Mapped[str] = mapped_column(String(255))
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str | None] = mapped_column(Text)


class ConnectionRow(Base):
    __tablename__ = "hub_connection_leases"

    connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(255), index=True)
    connector_id: Mapped[str] = mapped_column(String(255))
    connector_kind: Mapped[str] = mapped_column(String(32))
    signaling_ref: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[str] = mapped_column(Text)
    identity_fingerprint: Mapped[str] = mapped_column(String(512))
    hub_instance_id: Mapped[str] = mapped_column(String(255), index=True)
    fencing_token: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    heartbeat_sequence: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (Index("ix_hub_connections_active", "device_id", "state", "expires_at"),)


class DeviceAuthorityRow(Base):
    __tablename__ = "hub_device_authorities"

    device_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    hub_instance_id: Mapped[str] = mapped_column(String(255), index=True)
    fencing_token: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ChallengeRow(Base):
    __tablename__ = "hub_enrollment_challenges"

    challenge_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(255), index=True)
    client_nonce: Mapped[str] = mapped_column(Text)
    server_nonce: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    connector_id: Mapped[str] = mapped_column(String(255))
    connector_kind: Mapped[str] = mapped_column(String(32))
    signaling_ref: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class DirectoryRow(Base):
    __tablename__ = "hub_device_directory"

    device_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_scope: Mapped[str] = mapped_column(String(255), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (Index("ix_hub_directory_owner_device", "owner_scope", "device_id"),)


class ChannelLeaseRow(Base):
    __tablename__ = "hub_channel_leases"

    channel_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(255), index=True)
    profile_name: Mapped[str] = mapped_column(String(255), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    renew_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_hub_channels_active", "device_id", "profile_name", "expires_at"),)


class ChannelCursorRow(Base):
    __tablename__ = "hub_channel_cursors"

    channel_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    outbound_sequence: Mapped[int] = mapped_column(Integer, default=0)
    inbound_sequence: Mapped[int] = mapped_column(Integer, default=0)
    inbound_envelope_id: Mapped[str] = mapped_column(String(255), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)


class EventRow(Base):
    __tablename__ = "hub_events"

    stream_position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(512), index=True)
    owner_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    subject_type: Mapped[str] = mapped_column(String(64), default="")
    subject_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_json: Mapped[str] = mapped_column(Text)
