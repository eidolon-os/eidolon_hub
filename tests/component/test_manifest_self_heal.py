"""A device with a stale Manifest repairs itself, end to end, over HTTP.

This is the loop the whole design change exists for, exercised against real
persistence, the real router and real P-256 signatures — no stubbed store, no
stubbed crypto.

The device that motivated it enrolled declaring `{"endpoints": []}`: it told
the Host it could carry nothing. That declaration was frozen into its Claim,
the Channel Provider went on provisioning from it, and the device sat in
WaitingBinding indefinitely. The only repair available was a removal that
requires a person standing at the hardware.
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
    device_control_configuration_proof_document,
    manifest_digest,
)
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.channel_reconciliation.application import ReconcileChannelBinding
from hub.channel_reconciliation.domain import ChannelBinding, CurrentChannelBinding
from hub.device_control.application import AcceptDeviceManifest, PullDeviceConfiguration
from hub.device_control.http import DeviceEraseHttpServices, create_device_erase_router
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

#: What the device declared at enrollment, and could never take back.
PLACEHOLDER = {"endpoints": []}
#: What the firmware that fixed the defect actually builds.
REAL = {
    "schema_version": 1,
    "actions": [],
    "events": [],
    "media": [{"codecs": ["opus"], "direction": "bidirectional", "kind": "audio"}],
    "properties": [],
    "title": "esp32-s3-touch-amoled-2.06",
}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _spki(key: ec.EllipticCurvePrivateKey) -> str:
    return _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def _sign(key: ec.EllipticCurvePrivateKey, document: dict[str, object]) -> str:
    r, s = decode_dss_signature(key.sign(canonical_bytes(document), ec.ECDSA(hashes.SHA256())))
    return _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:04d}"


class _ClaimReader:
    """Admission's Claim projection, which Device Control only ever reads."""

    def __init__(self, spki: str) -> None:
        self._spki = spki

    async def get_exact(self, *, device_ref):
        if device_ref != REF:
            return None
        # Stored the way Admission spells it, which is not how the device sends it.
        return DeviceClaimProjection(
            device_ref=REF,
            state="active",
            operational_public_key_spki="p256-spki:" + self._spki,
        )


class _Provider:
    """A Channel Provider that will only bind a device that can carry audio."""

    def __init__(self) -> None:
        self.provisioned: list[dict] = []
        self._binding = None

    async def current(self, *, device_ref):
        return self._binding

    async def provision(self, *, operation_id, manifest, manifest_revision, **_kw):
        self.provisioned.append({"operation_id": operation_id, "manifest": manifest})
        kinds = tuple(
            item["kind"]
            for item in manifest.get("media", ())
            if isinstance(item, dict) and item.get("kind")
        )
        if not kinds:
            raise ValueError("nothing to provision: the device declares no media")
        channels = (
            ChannelBinding(
                channel_id="channel_01",
                purpose="device-session",
                kinds=kinds,
                binding_format="application/vnd.eidolon.livekit-session+json;v=2",
                issued_at_ms=1,
                expires_at_ms=1_900_000_000_000,
                opaque_binding="e30=",
            ),
        )
        self._binding = CurrentChannelBinding(
            operation_id=operation_id,
            manifest_revision=manifest_revision,
            channels=channels,
        )
        return channels

    async def refresh(self, **kwargs):  # pragma: no cover - not reached here
        raise AssertionError("a live binding was not expected to need refreshing")


@pytest.fixture
async def hub(tmp_path):
    database = HubDatabase.sqlite(
        tmp_path / "hub.sqlite3",
        owner_domain_id=str(REF.owner_domain_id),
        owner_domain_generation=REF.owner_domain_generation,
    )
    await database.initialize_schema()
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_a_device_that_declared_nothing_repairs_itself_and_gets_a_binding(hub) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    repositories = SqlHubRepositories(hub)
    provider = _Provider()

    # The Claim as it stands: active, and carrying the placeholder declaration.
    await repositories.device_mutations.commit(
        expected=None,
        device=ManagedDevice(
            identity=DeviceIdentity(REF.device_instance_id),
            display_name="Box",
            manifest_id="esp32-s3-touch-amoled-2.06",
            manifest=DeviceManifestDocument.from_declaration(
                document=PLACEHOLDER, declared_revision=1
            ),
            enrolled_at=NOW,
            updated_at=NOW,
            owner_domain_id=str(REF.owner_domain_id),
            owner_domain_generation=REF.owner_domain_generation,
            claim_generation=REF.claim_generation,
            trust_epoch=REF.trust_epoch,
            owner_id="owner_01",
            lifecycle_state=DeviceLifecycleState.APPROVED,
        ),
        event=_claim_event(),
    )

    directory = ProjectDeviceDirectory(
        devices=repositories.devices, directory=InMemoryDeviceDirectoryRepository()
    )
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                configuration=PullDeviceConfiguration(
                    claims=_ClaimReader(_spki(key)), devices=repositories.devices
                ),
                manifest=AcceptDeviceManifest(
                    claims=_ClaimReader(_spki(key)),
                    devices=repositories.devices,
                    mutations=repositories.device_mutations,
                    directory=directory,
                    ids=_Ids(),
                    clock=_Clock(),
                ),
                channel_binding=ReconcileChannelBinding(
                    devices=repositories.devices, provider=provider, clock=_Clock()
                ),
                pull=_Unused(),
                acknowledge=_Unused(),
                reconcile=_Unused(),
                ledger=_Unused(),
                secret=b"m" * 32,
            )
        )
    )

    with TestClient(app) as client:
        # 1. The device asks for its configuration, as it does every few seconds.
        first = _configuration(client, key, nonce="configuration_nonce_1")
        assert first["channels"] == [], "a device declaring nothing cannot be bound"
        assert first["manifest"] == {
            "manifest_id": "esp32-s3-touch-amoled-2.06",
            "revision": 1,
            "digest": manifest_digest(PLACEHOLDER),
        }

        # 2. It disagrees, and says so at the revision after the one it was told.
        assert first["manifest"]["digest"] != manifest_digest(REAL)
        acceptance = _assert_manifest(
            client, key, revision=first["manifest"]["revision"] + 1, document=REAL
        )
        assert acceptance["outcome"] == "accepted"
        assert acceptance["accepted"]["revision"] == 2

        # 3. The next configuration answer is bound, because the Provider now has
        #    a Manifest it can provision from. Nothing else had to be told.
        second = _configuration(client, key, nonce="configuration_nonce_2")
        assert [channel["kinds"] for channel in second["channels"]] == [["audio"]]
        assert second["manifest"]["revision"] == 2
        assert second["manifest"]["digest"] == manifest_digest(REAL)

        # 4. And it settles: re-asserting the same thing is not a change, and the
        #    binding is not re-provisioned under a new operation id.
        repeat = _assert_manifest(client, key, revision=2, document=REAL)
        assert repeat["outcome"] == "unchanged"
        third = _configuration(client, key, nonce="configuration_nonce_3")
        assert third["channels"] == second["channels"]

    # The Provider was asked twice under the placeholder-free identity and never
    # under a changed one in between: the operation id is keyed on the digest.
    operations = {entry["operation_id"] for entry in provider.provisioned}
    assert len(operations) == 2, "one provision per distinct Manifest, and no more"

    stored = await repositories.devices.get(REF.device_instance_id)
    assert stored.manifest.declared_revision == 2
    assert stored.manifest.digest == manifest_digest(REAL)

    projected = await directory.execute(REF.device_instance_id)
    assert projected.manifest_digest == manifest_digest(REAL)


def _configuration(client, key, *, nonce: str) -> dict:
    document = device_control_configuration_proof_document(
        device_ref=REF,
        nonce=nonce,
    )
    response = client.post(
        "/api/device-control/v1/configuration:pull",
        json={
            "device_ref": REF.model_dump(mode="json"),
            "nonce": nonce,
            "public_key_spki": _spki(key),
            "device_signature": _sign(key, document),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_manifest(client, key, *, revision: int, document: dict) -> dict:
    manifest = ManifestDocument(
        manifest_id="esp32-s3-touch-amoled-2.06",
        revision=revision,
        digest=manifest_digest(document),
        document=document,
    )
    unsigned = AssertDeviceManifest(
        device_ref=REF,
        manifest=manifest,
        nonce=f"manifest_nonce_{revision:04d}",
        public_key_spki=_spki(key),
        device_signature="A" * 86,
    )
    signed = unsigned.model_copy(
        update={"device_signature": _sign(key, unsigned.signing_document())}
    )
    response = client.post(
        "/api/device-control/v1/manifest:assert", json=signed.model_dump(mode="json")
    )
    assert response.status_code == 200, response.text
    return response.json()


class _Unused:
    def __getattr__(self, name):
        raise AssertionError(f"{name} is not part of this loop")


def _claim_event():
    from hub.ports.management_events import DeviceManagementEventRecord

    return DeviceManagementEventRecord(
        event_id="claim-activated_0001",
        event_type="live.eidolon.device.claim-activated.v1",
        source="urn:eidolon:authority:admission",
        principal_id=REF.device_instance_id,
        subject=REF.device_instance_id,
        occurred_at=NOW,
        data={"manifest_revision": manifest_digest(PLACEHOLDER)},
    )
