"""Relational schema owned by the local Eidolon Hub."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuthorityStateRow(Base):
    """Singleton binding this database to one Owner Authority lineage."""

    __tablename__ = "hub_authority_state"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_domain_id: Mapped[str] = mapped_column(String(255))
    owner_domain_generation: Mapped[int] = mapped_column(Integer)
    state_id: Mapped[str] = mapped_column(String(128), unique=True)


class DeviceRow(Base):
    """Owner-facing directory projection; Admission remains authoritative."""

    __tablename__ = "hub_device_directory_v1"

    device_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(512))
    device_kind: Mapped[str] = mapped_column(String(255), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    # The content digest, which identifies which document this is. Named for
    # the wire field the Channel Provider keys its binding cache on.
    manifest_revision: Mapped[str] = mapped_column(String(80))
    # The device's own count of how many times its capabilities have changed.
    manifest_declared_revision: Mapped[int] = mapped_column(Integer, default=1)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    owner_domain_generation: Mapped[int] = mapped_column(Integer, default=1)
    claim_generation: Mapped[int] = mapped_column(Integer, default=1)
    trust_epoch: Mapped[int] = mapped_column(Integer, default=1)
    aggregate_revision: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[str | None] = mapped_column(String(255), index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32), index=True)
    last_management_request_id: Mapped[str] = mapped_column(String(255), default="")
    last_management_fingerprint: Mapped[str] = mapped_column(String(128), default="")


class DeviceManagementEventRow(Base):
    __tablename__ = "hub_events"

    stream_position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(512), default="")
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(512), index=True)
    owner_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_json: Mapped[str] = mapped_column(Text)


class ChannelRevocationDeliveryRow(Base):
    """Durable delivery state; Provider remains authoritative for Channel state."""

    __tablename__ = "hub_channel_revocation_delivery_v1"

    source_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), unique=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    owner_domain_generation: Mapped[int] = mapped_column(Integer)
    claim_generation: Mapped[int] = mapped_column(Integer)
    trust_epoch: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(24), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_code: Mapped[str] = mapped_column(String(128), default="")
    last_error: Mapped[str] = mapped_column(String(512), default="")


class DeviceEraseOperationRow(Base):
    """Independent device-local.erase ledger; never stores Channel state."""

    __tablename__ = "hub_device_erase_operations"

    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_event_id: Mapped[str] = mapped_column(String(255), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(128))
    device_id: Mapped[str] = mapped_column(String(255), index=True)
    owner_domain_id: Mapped[str] = mapped_column(String(255), index=True)
    owner_domain_generation: Mapped[int] = mapped_column(Integer)
    claim_generation: Mapped[int] = mapped_column(Integer)
    trust_epoch: Mapped[int] = mapped_column(Integer)
    accepted_manifest_digest: Mapped[str] = mapped_column(String(128))
    public_key_spki: Mapped[str | None] = mapped_column(String(256), nullable=True)
    key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    command_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    delivery_attempt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_code: Mapped[str] = mapped_column(String(128), default="")
    last_error_code: Mapped[str] = mapped_column(String(128), default="")


class DeviceEraseAckEvidenceRow(Base):
    """Immutable ACK/late-evidence idempotency record."""

    __tablename__ = "hub_device_erase_ack_evidence"

    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    ack_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    ack_fingerprint: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(64))
    result_code: Mapped[str] = mapped_column(String(128))


class AdmissionBaseIdentityRow(Base):
    """One issued base identity and the operational key it is bound to.

    This is the whole of what replaced a per-device secret on both sides. The
    binding is one-to-one and permanent for the life of the identity: the same
    key may not acquire a second base identity, and a base identity may not be
    presented by a second key. A device that erased its storage has no row to
    match and is a new Body — recognising it by an unauthenticated MAC would be
    inheriting ownership from something nobody verified.
    """

    __tablename__ = "admission_base_identities_v1"

    device_base_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    hardware_identity_ref: Mapped[str] = mapped_column(String(128), index=True)
    operational_key_id: Mapped[str] = mapped_column(String(71), unique=True, index=True)
    provenance: Mapped[str] = mapped_column(String(32))
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionCommissioningVoucherRow(Base):
    """The consumed half of a one-shot voucher.

    Kept durably rather than in memory because the window a voucher is valid in
    outlives a Hub restart: an in-process ledger would make "restart the Hub"
    the way to replay one.
    """

    __tablename__ = "admission_commissioning_vouchers_v1"

    jti: Mapped[str] = mapped_column(String(256), primary_key=True)
    device_base_id: Mapped[str] = mapped_column(String(128), index=True)
    operational_key_id: Mapped[str] = mapped_column(String(71))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionProposalRow(Base):
    __tablename__ = "admission_proposals_v1"

    enrollment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    device_instance_id: Mapped[str] = mapped_column(String(128), index=True)
    hardware_identity_ref: Mapped[str] = mapped_column(String(128), index=True)
    requested_owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(48), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    hardware_evidence_digest: Mapped[str] = mapped_column(String(71))
    commissioning_proof_digest: Mapped[str] = mapped_column(String(71))
    manifest_id: Mapped[str] = mapped_column(String(128))
    manifest_revision: Mapped[int] = mapped_column(Integer)
    manifest_digest: Mapped[str] = mapped_column(String(71))
    manifest_json: Mapped[str] = mapped_column(Text)
    handoff_public_key_spki: Mapped[str] = mapped_column(Text)
    handoff_key_id: Mapped[str] = mapped_column(String(71))
    operational_public_key_spki: Mapped[str] = mapped_column(Text)
    operational_key_id: Mapped[str] = mapped_column(String(71))
    collection_challenge_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionDecisionRow(Base):
    __tablename__ = "admission_decisions_v1"

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    enrollment_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    decision: Mapped[str] = mapped_column(String(16))
    actor_json: Mapped[str] = mapped_column(Text)
    target_owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    target_business_owner_id: Mapped[str] = mapped_column(String(128), index=True)
    reviewed_manifest_json: Mapped[str] = mapped_column(Text)
    expected_proposal_revision: Mapped[int] = mapped_column(Integer)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionGrantRow(Base):
    __tablename__ = "admission_claim_grants_v1"

    grant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    enrollment_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    decision_id: Mapped[str] = mapped_column(String(128), unique=True)
    owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    hardware_identity_ref: Mapped[str] = mapped_column(String(128), index=True)
    claim_generation: Mapped[int] = mapped_column(Integer)
    device_ref_json: Mapped[str] = mapped_column(Text)
    manifest_ref_json: Mapped[str] = mapped_column(Text)
    handoff_key_id: Mapped[str] = mapped_column(String(71))
    operational_key_id: Mapped[str] = mapped_column(String(71))
    grant_json: Mapped[str] = mapped_column(Text)
    wire_envelope_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdmissionGrantAckRow(Base):
    __tablename__ = "admission_grant_acks_v1"

    grant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    enrollment_id: Mapped[str] = mapped_column(String(128), unique=True)
    proof_fingerprint: Mapped[str] = mapped_column(String(71))
    device_ref_json: Mapped[str] = mapped_column(Text)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionClaimRow(Base):
    __tablename__ = "admission_claims_v1"

    device_instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    business_owner_id: Mapped[str] = mapped_column(String(128), index=True)
    hardware_identity_ref: Mapped[str] = mapped_column(String(128), index=True)
    owner_domain_generation: Mapped[int] = mapped_column(Integer)
    claim_generation: Mapped[int] = mapped_column(Integer)
    trust_epoch: Mapped[int] = mapped_column(Integer)
    manifest_ref_json: Mapped[str] = mapped_column(Text)
    approval_decision_id: Mapped[str] = mapped_column(String(128))
    operational_public_key_spki: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdmissionCommandResultRow(Base):
    __tablename__ = "admission_command_results_v1"

    owner_domain_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(96), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(71))
    result_json: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionOutboxRow(Base):
    __tablename__ = "admission_outbox_v1"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(128))
    aggregate_id: Mapped[str] = mapped_column(String(128), index=True)
    aggregate_revision: Mapped[int] = mapped_column(Integer)
    event_json: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(512), default="")


class AdmissionClaimEventStreamRow(Base):
    """Dedicated contiguous recovery stream; transport position is not event data."""

    __tablename__ = "admission_claim_event_stream_v1"
    __table_args__ = {"sqlite_autoincrement": True}

    stream_position: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    owner_domain_id: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    event_json: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
