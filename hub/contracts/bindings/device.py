"""Runtime normalization bindings for device wire contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from eidolon_sdk.device_foundation.v1 import (
    ClaimEventPage,
    DeviceEraseContractError,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceLocalEraseOperationStatus,
    DeviceOperationKeyProof,
    DeviceRef,
    OwnerDomainId,
    canonical_bytes,
    operation_fingerprint,
    operation_key_id,
    revoke_claim_fingerprint,
    verify_device_erase_ack,
    verify_operation_key_proof,
    verify_p256_signature,
)
from eidolon_sdk.device_foundation.v1 import (
    ClaimEventRecord as ClaimEvent,
)
from eidolon_sdk.device_foundation.v1 import (
    RevokeClaim as DeviceRevocationRequest,
)
from eidolon_sdk.device_foundation.v1 import (
    RevokeClaimResult as ClaimRevocationResult,
)
from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel, JsonObject

DeviceLifecycleState = Literal["pending-approval", "approved", "revoked"]

__all__ = [
    "ClaimEvent",
    "ClaimEventPage",
    "ClaimRevocationResult",
    "DeviceControlOperationStatus",
    "DeviceEraseContractError",
    "DeviceLocalEraseAck",
    "DeviceLocalEraseCommand",
    "DeviceLocalEraseOperationStatus",
    "DeviceOperationKeyProof",
    "DeviceRef",
    "OwnerDomainId",
    "DeviceRevocationRequest",
    "canonical_bytes",
    "operation_fingerprint",
    "operation_key_id",
    "revoke_claim_fingerprint",
    "verify_device_erase_ack",
    "verify_operation_key_proof",
    "verify_p256_signature",
]


class LegacyDeviceRef(ContractModel):
    """Existing PH1 wire shape kept only on routes awaiting PH2-B cutover."""

    device_instance_id: str = Field(min_length=1, max_length=128)
    owner_domain_id: str = Field(min_length=1, max_length=128)
    owner_domain_generation: int = Field(ge=1)
    claim_generation: int = Field(ge=1)
    trust_epoch: int = Field(ge=1)
    accepted_manifest_digest: str = Field(min_length=1, max_length=128)


class LegacyDeviceRevocationRequest(ContractModel):
    """Existing management route input; removed by the PH2-B cutover."""

    operation: Literal["device.claim-revocation"] = "device.claim-revocation"
    command_id: str = Field(min_length=3, max_length=128)
    correlation_id: str = Field(min_length=3, max_length=128)
    device_ref: LegacyDeviceRef
    reason: str = Field(min_length=1, max_length=256)


class LegacyClaimRevocationResult(ContractModel):
    operation: Literal["device.claim-revocation-result"] = (
        "device.claim-revocation-result"
    )
    command_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["committed", "replayed"]
    device_ref: LegacyDeviceRef
    aggregate_revision: int = Field(ge=1)
    occurred_at: datetime
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    lifecycle_state: Literal["revoked"] = "revoked"


class LegacyClaimEvent(ContractModel):
    operation: Literal["device.claim-event"] = "device.claim-event"
    stream_position: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["live.eidolon.device.claim-revoked.v1"]
    device_ref: LegacyDeviceRef
    aggregate_revision: int = Field(ge=1)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    reason: str = Field(min_length=1, max_length=256)


class LegacyClaimEventPage(ContractModel):
    operation: Literal["device.claim-event-page"] = "device.claim-event-page"
    next_stream_position: int = Field(ge=0)
    events: tuple[LegacyClaimEvent, ...] = Field(default=(), max_length=500)

    @field_validator("events", mode="before")
    @classmethod
    def _event_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class PropertyAffordance(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    schema_: JsonObject = Field(alias="schema")
    observable: bool = False
    writable: bool = False


class ActionAffordance(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1, le=65_535)
    input_schema: JsonObject
    output_schema: JsonObject
    idempotent: bool = False


class EventAffordance(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    data_schema: JsonObject


class MediaCapability(ContractModel):
    kind: Literal["audio", "video"]
    direction: Literal["publish", "subscribe", "bidirectional"]
    codecs: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("codecs", mode="before")
    @classmethod
    def _codec_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceManifest(ContractModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=128)
    properties: tuple[PropertyAffordance, ...] = Field(default=(), max_length=64)
    actions: tuple[ActionAffordance, ...] = Field(default=(), max_length=64)
    events: tuple[EventAffordance, ...] = Field(default=(), max_length=64)
    media: tuple[MediaCapability, ...] = Field(default=(), max_length=16)

    @field_validator("properties", "actions", "events", "media", mode="before")
    @classmethod
    def _json_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceApprovalRequest(ContractModel):
    operation: Literal["device.approval"] = "device.approval"
    request_id: str = Field(min_length=1, max_length=96)
    owner_id: str = Field(min_length=1, max_length=64)


class DeviceRenameRequest(ContractModel):
    """What an Owner calls a device, as the only part of it they decide."""

    operation: Literal["device.rename"] = "device.rename"
    display_name: str = Field(min_length=1, max_length=128)
    #: Whose device the caller believes this is; the Hub refuses to rename one
    #: held by anyone else.
    owner_scope: str | None = Field(default=None, min_length=1, max_length=64)


class DeviceLifecycleStatus(ContractModel):
    operation: Literal["device.lifecycle-status"] = "device.lifecycle-status"
    device_id: str = Field(min_length=1, max_length=128)
    owner_id: str | None = Field(default=None, max_length=64)
    lifecycle_state: DeviceLifecycleState


class DeviceControlOperationStatus(ContractModel):
    """Read-only status of the current Channel delivery projection.

    This is deliberately not the future device-local erase contract. A
    successful Channel delivery proves platform channel access was revoked;
    it does not prove that an offline device erased local state.
    """

    operation: Literal["device-control.operation-status"] = (
        "device-control.operation-status"
    )
    event_id: str = Field(min_length=3, max_length=128)
    operation_id: str = Field(min_length=3, max_length=255)
    operation_type: Literal["channel.device-access.revoke"]
    device_ref: DeviceRef
    state: Literal["pending", "delivered"]
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime
    delivered_at: datetime | None = None
    last_error: str = Field(default="", max_length=512)


class DeviceDirectoryEntry(ContractModel):
    operation: Literal["device.directory-entry"] = "device.directory-entry"
    device_id: str = Field(min_length=1, max_length=128)
    owner_scope: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest: DeviceManifest
    manifest_revision: str = Field(min_length=1, max_length=128)
    lifecycle_state: DeviceLifecycleState
    enrolled_at: datetime
    updated_at: datetime
    device_ref: LegacyDeviceRef | None = None


class DeviceDirectoryPage(ContractModel):
    operation: Literal["device.directory-page"] = "device.directory-page"
    next_cursor: str | None = Field(default=None, max_length=128)
    devices: tuple[DeviceDirectoryEntry, ...] = Field(default=(), max_length=100)

    @field_validator("devices", mode="before")
    @classmethod
    def _device_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value


class DeviceManagementEvent(ContractModel):
    operation: Literal["device.management-event"] = "device.management-event"
    stream_position: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=512)
    principal_id: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=255)
    occurred_at: datetime
    data: JsonObject


class DeviceManagementEventPage(ContractModel):
    operation: Literal["device.management-event-page"] = "device.management-event-page"
    next_stream_position: int = Field(ge=0)
    events: tuple[DeviceManagementEvent, ...] = Field(default=(), max_length=500)

    @field_validator("events", mode="before")
    @classmethod
    def _event_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value
