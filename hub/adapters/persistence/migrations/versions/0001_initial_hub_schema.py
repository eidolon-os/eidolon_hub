"""Initial Hub-owned device access and management schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hub_devices",
        sa.Column("device_id", sa.String(255), primary_key=True),
        sa.Column("public_key_fingerprint", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("device_kind", sa.String(255), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_revision", sa.String(80), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_registration_request_id", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("last_management_request_id", sa.String(255), nullable=False),
        sa.Column("last_management_fingerprint", sa.String(128), nullable=False),
    )
    for column in ("tenant_id", "device_kind", "updated_at", "owner_id", "approved", "revoked"):
        op.create_index(f"ix_hub_devices_{column}", "hub_devices", [column])

    op.create_table(
        "hub_commands",
        sa.Column("command_id", sa.String(255), primary_key=True),
        sa.Column("device_id", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
    )
    for column in ("device_id", "state", "expires_at"):
        op.create_index(f"ix_hub_commands_{column}", "hub_commands", [column])

    op.create_table(
        "hub_device_sessions",
        sa.Column("session_id", sa.String(255), primary_key=True),
        sa.Column("device_id", sa.String(255), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Text(), nullable=False),
        sa.Column("identity_fingerprint", sa.String(512), nullable=False),
        sa.Column("hub_instance_id", sa.String(255), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("heartbeat_sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    for column in ("device_id", "expires_at", "hub_instance_id", "state"):
        op.create_index(f"ix_hub_device_sessions_{column}", "hub_device_sessions", [column])
    op.create_index(
        "ix_hub_sessions_active",
        "hub_device_sessions",
        ["device_id", "state", "expires_at"],
    )

    op.create_table(
        "hub_device_authorities",
        sa.Column("device_id", sa.String(255), primary_key=True),
        sa.Column("hub_instance_id", sa.String(255), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    for column in ("hub_instance_id", "expires_at"):
        op.create_index(f"ix_hub_device_authorities_{column}", "hub_device_authorities", [column])

    op.create_table(
        "hub_enrollment_challenges",
        sa.Column("challenge_id", sa.String(255), primary_key=True),
        sa.Column("device_id", sa.String(255), nullable=False),
        sa.Column("client_nonce", sa.Text(), nullable=False),
        sa.Column("server_nonce", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    for column in ("device_id", "expires_at"):
        op.create_index(
            f"ix_hub_enrollment_challenges_{column}", "hub_enrollment_challenges", [column]
        )

    op.create_table(
        "hub_device_directory",
        sa.Column("device_id", sa.String(255), primary_key=True),
        sa.Column("owner_scope", sa.String(255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    for column in ("owner_scope", "updated_at"):
        op.create_index(f"ix_hub_device_directory_{column}", "hub_device_directory", [column])
    op.create_index(
        "ix_hub_directory_owner_device",
        "hub_device_directory",
        ["owner_scope", "device_id"],
    )

    op.create_table(
        "hub_channel_assignments",
        sa.Column("channel_id", sa.String(255), primary_key=True),
        sa.Column("device_id", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(255), nullable=False),
        sa.Column("kinds", sa.Text(), nullable=False),
        sa.Column("binding_format", sa.String(128), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("device_id", "purpose", "expires_at", "state"):
        op.create_index(f"ix_hub_channel_assignments_{column}", "hub_channel_assignments", [column])
    op.create_index(
        "ix_hub_channels_active",
        "hub_channel_assignments",
        ["device_id", "purpose", "state", "expires_at"],
    )

    op.create_table(
        "hub_channel_cursors",
        sa.Column("channel_id", sa.String(255), primary_key=True),
        sa.Column("outbound_sequence", sa.Integer(), nullable=False),
        sa.Column("inbound_sequence", sa.Integer(), nullable=False),
        sa.Column("inbound_envelope_id", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )

    op.create_table(
        "hub_events",
        sa.Column("stream_position", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_hub_events_event_id", "hub_events", ["event_id"], unique=True)
    for column in ("event_type", "subject", "owner_id", "occurred_at"):
        op.create_index(f"ix_hub_events_{column}", "hub_events", [column])


def downgrade() -> None:
    for table in (
        "hub_events",
        "hub_channel_cursors",
        "hub_channel_assignments",
        "hub_device_directory",
        "hub_enrollment_challenges",
        "hub_device_authorities",
        "hub_device_sessions",
        "hub_commands",
        "hub_devices",
    ):
        op.drop_table(table)
