"""Runtime normalization bindings for device wire contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from eidolon_sdk.device_foundation.v1 import (
    AssertDeviceManifest,
    DeliverEnvelope,
    DeliveryAcceptance,
    DeviceEraseContractError,
    DeviceEvidenceEnvelope,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceLocalEraseOperationStatus,
    DeviceManifestAcceptance,
    DeviceOperationKeyProof,
    DeviceRef,
    ManifestRef,
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
    RevokeClaim as DeviceRevocationRequest,
)
from eidolon_sdk.device_foundation.v1 import (
    RevokeClaimResult as ClaimRevocationResult,
)
from pydantic import Field, field_validator

from hub.contracts.bindings.common import ContractModel, JsonObject

DeviceLifecycleState = Literal["approved", "revoked"]

__all__ = [
    "AssertDeviceManifest",
    "ClaimRevocationResult",
    "DeviceManifestAcceptance",
    "ManifestRef",
    "DeviceEraseContractError",
    "DeviceLocalEraseAck",
    "DeviceLocalEraseCommand",
    "DeviceLocalEraseOperationStatus",
    "DeliverEnvelope",
    "DeliveryAcceptance",
    "DeviceEvidenceEnvelope",
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
    """How the owner-facing directory *reads* a Manifest it already stored.

    Not a second definition of the Manifest contract. The definition lives in
    `eidolon_sdk`'s `DeviceCapabilityManifest`, which both entry points check a
    proposed document against, and this must accept everything that admits —
    `test_the_directory_reads_every_document_the_entry_admits` is the gate.

    It is deliberately the looser of the two, and must stay looser. This parses
    a document the Authority accepted at some point in the past, possibly under
    an entry that checked less than today's does, and tightening a *reader of
    stored history* is how a projection row became able to kill the Authority:
    a document Hub had already admitted made the directory raise on every boot.
    Requiredness therefore belongs at the entry, where a device is still asking
    and can be answered, and never here.
    """

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


class ForeignDeviceManifest(ContractModel):
    """The Authority holds a Manifest, and this vocabulary cannot read it.

    Not an error, and not a device that declares nothing. A Manifest is an
    opaque document to the Authority, and the entry that admits one has widened
    over time, so the directory necessarily reads documents authored against
    vocabularies other than its own — the first device ever claimed canonically
    sent `{"endpoints": []}`, and the Authority accepted it.

    Saying so is the only honest projection of that state. Raising takes the
    Owner's device page down for a device Hub itself admitted, which is the
    boot-loop incident wearing different clothes. `null` cannot be told apart
    from a device with nothing to declare. Projecting the fields that happen to
    parse reports an empty capability set for a device that has one, and has to
    invent a `title` to do it.

    The entry's `manifest_revision` still names exactly which document this is,
    so the row stays actionable: the Owner sees their device, and an operator
    can find the document `detail` describes.
    """

    manifest_kind: Literal["foreign"] = "foreign"
    #: Why this vocabulary could not read the document, for a person to read.
    #: Field paths and messages are diagnostic and may change; nothing should
    #: branch on the text. What is stable is that this member is present at all.
    detail: str = Field(min_length=1, max_length=512)


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


class DeviceDirectoryEntry(ContractModel):
    operation: Literal["device.directory-entry"] = "device.directory-entry"
    device_id: str = Field(min_length=1, max_length=128)
    owner_scope: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=128)
    device_kind: str = Field(min_length=1, max_length=96)
    manifest: DeviceManifest | ForeignDeviceManifest
    manifest_revision: str = Field(min_length=1, max_length=128)
    lifecycle_state: DeviceLifecycleState
    enrolled_at: datetime
    updated_at: datetime
    device_ref: DeviceRef | None = None


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
