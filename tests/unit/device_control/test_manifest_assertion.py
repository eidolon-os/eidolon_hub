"""A claimed device may correct and update its own capability Manifest.

The Manifest was previously written once, by the Claim that admitted the
device, and never again. A device whose declaration was wrong at enrollment —
or simply older than its current firmware — had no way to say so, and the
Authority went on provisioning against a document the device had outgrown.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from eidolon_sdk.device_foundation.v1 import (
    AssertDeviceManifest,
    DeviceRef,
    ManifestDocument,
    canonical_bytes,
    manifest_digest,
)
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from hub.device_control.application import AcceptDeviceManifest
from hub.device_control.domain import ManifestRevisionConflict
from hub.device_control.ports import DeviceClaimProjection
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_01 = named_device_instance_id("device_01")

NOW = datetime(2026, 8, 25, tzinfo=UTC)
REF = DeviceRef(
    device_instance_id=_DEVICE_01,
    owner_domain_id="owner-domain_01",
    owner_domain_generation=1,
    claim_generation=2,
    trust_epoch=1,
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _document(*, camera: bool) -> dict[str, object]:
    media = [{"kind": "audio"}] + ([{"kind": "video"}] if camera else [])
    return {"schema_version": 1, "media": media}


def _manifest(*, revision: int, camera: bool) -> ManifestDocument:
    document = _document(camera=camera)
    return ManifestDocument(
        manifest_id="esp-box-3",
        revision=revision,
        digest=manifest_digest(document),
        document=document,
    )


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Ids:
    def __init__(self) -> None:
        self.count = 0

    def new(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}_{self.count:02d}"


class _ClaimReader:
    def __init__(self, projection: DeviceClaimProjection | None) -> None:
        self._projection = projection

    async def get_exact(self, *, device_ref):
        if self._projection is None or device_ref != self._projection.device_ref:
            return None
        return self._projection


class _Devices:
    def __init__(self, device: ManagedDevice) -> None:
        self.device = device

    async def get(self, device_id: str) -> ManagedDevice | None:
        return self.device if device_id == self.device.identity.device_id else None


class _Directory:
    """The owner-facing projection, which must be told what changed."""

    def __init__(self) -> None:
        self.projected: list[str] = []

    async def execute(self, device_id: str):
        self.projected.append(device_id)
        return device_id


class _Mutations:
    def __init__(self, devices: _Devices) -> None:
        self._devices = devices
        self.events: list[object] = []

    async def commit(self, *, expected, device, event):
        assert expected == self._devices.device, "manifest acceptance must be CAS-guarded"
        self._devices.device = device
        self.events.append(event)
        return device


def _device(manifest: ManifestDocument) -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity(REF.device_instance_id),
        display_name="Box",
        device_kind=manifest.manifest_id,
        manifest=DeviceManifestDocument.from_declaration(
            document=manifest.document, declared_revision=manifest.revision
        ),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_domain_id=str(REF.owner_domain_id),
        owner_domain_generation=REF.owner_domain_generation,
        claim_generation=REF.claim_generation,
        trust_epoch=REF.trust_epoch,
        owner_id="owner_01",
        lifecycle_state=DeviceLifecycleState.APPROVED,
    )


def _assertion(manifest: ManifestDocument, key: ec.EllipticCurvePrivateKey) -> AssertDeviceManifest:
    spki = _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    unsigned = AssertDeviceManifest(
        device_ref=REF,
        manifest=manifest,
        nonce="assertion_nonce_0001",
        public_key_spki=spki,
        device_signature="A" * 86,
    )
    der = key.sign(canonical_bytes(unsigned.signing_document()), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return unsigned.model_copy(
        update={"device_signature": _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}
    )


def _use_case(devices: _Devices, key: ec.EllipticCurvePrivateKey, *, state: str = "active"):
    spki = _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    mutations = _Mutations(devices)
    directory = _Directory()
    return (
        AcceptDeviceManifest(
            claims=_ClaimReader(
                DeviceClaimProjection(device_ref=REF, state=state, operational_public_key_spki=spki)
            ),
            devices=devices,
            mutations=mutations,
            directory=directory,
            ids=_Ids(),
            clock=_Clock(),
        ),
        mutations,
        directory,
    )


@pytest.mark.asyncio
async def test_a_later_revision_replaces_what_the_authority_had_accepted() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    devices = _Devices(_device(_manifest(revision=1, camera=False)))
    accept, mutations, directory = _use_case(devices, key)

    upgraded = _manifest(revision=2, camera=True)
    acceptance = await accept.execute(assertion=_assertion(upgraded, key))

    assert acceptance.outcome == "accepted"
    assert acceptance.accepted == upgraded.ref
    assert devices.device.manifest.declared_revision == 2
    assert devices.device.manifest.digest == upgraded.digest
    assert devices.device.manifest.declares_capability("video")
    # The Channel binding is keyed on the digest, so a changed Manifest must
    # change it: that is the whole reason a device is allowed to re-assert.
    assert devices.device.manifest.digest != _manifest(revision=1, camera=False).digest
    assert [event.event_type for event in mutations.events] == [
        "live.eidolon.device.manifest-accepted.v1"
    ]
    assert directory.projected == [REF.device_instance_id]


@pytest.mark.asyncio
async def test_reasserting_the_same_manifest_is_not_a_change() -> None:
    """A device asserts on every boot. The common answer must be cheap."""

    key = ec.generate_private_key(ec.SECP256R1())
    current = _manifest(revision=3, camera=True)
    devices = _Devices(_device(current))
    accept, mutations, directory = _use_case(devices, key)

    acceptance = await accept.execute(assertion=_assertion(current, key))

    assert acceptance.outcome == "unchanged"
    assert acceptance.accepted == current.ref
    assert mutations.events == []
    assert directory.projected == []


@pytest.mark.asyncio
async def test_an_older_revision_can_never_reinstate_itself() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    devices = _Devices(_device(_manifest(revision=5, camera=True)))
    accept, _, _directory = _use_case(devices, key)

    with pytest.raises(ManifestRevisionConflict):
        await accept.execute(assertion=_assertion(_manifest(revision=4, camera=False), key))
    assert devices.device.manifest.declared_revision == 5


@pytest.mark.asyncio
async def test_one_revision_cannot_describe_two_different_manifests() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    devices = _Devices(_device(_manifest(revision=5, camera=True)))
    accept, _, _directory = _use_case(devices, key)

    with pytest.raises(ManifestRevisionConflict):
        await accept.execute(assertion=_assertion(_manifest(revision=5, camera=False), key))


@pytest.mark.asyncio
async def test_a_manifest_is_only_accepted_from_the_claimed_key_and_an_active_claim() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    stranger = ec.generate_private_key(ec.SECP256R1())
    devices = _Devices(_device(_manifest(revision=1, camera=False)))

    accept, _, _directory = _use_case(devices, key)
    with pytest.raises(PermissionError):
        await accept.execute(assertion=_assertion(_manifest(revision=2, camera=True), stranger))

    revoked, _, _ = _use_case(devices, key, state="revoked")
    with pytest.raises(KeyError):
        await revoked.execute(assertion=_assertion(_manifest(revision=2, camera=True), key))
    assert devices.device.manifest.declared_revision == 1
