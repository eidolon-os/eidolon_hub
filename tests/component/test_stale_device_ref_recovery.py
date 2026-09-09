"""A device whose DeviceRef fell behind its own Claim is told, not refused.

The incident: an Owner re-added a Body that was already claimed. Admission
upserted the Claim at the next generation, which is what a re-grant is. The
device kept the ref it was handed at the previous one, and from that moment
every `configuration:pull` it made answered 409 forever, while the Claim row
said `active` and every Owner-facing surface reported health.

Nothing about that state is ambiguous to the Host. It holds one Claim for one
`device_instance_id`, that Claim is active, and the operational key the caller
signed with is the key that Claim records. The only thing the device has wrong
is a copy of a number the Host is authoritative for — and the answer to this
call already carries a `device_ref` field whose whole purpose is to say what
that number is. Before this change that field could only ever echo the request
back, so it could never inform anybody of anything.

These tests run against the real router, real SQLite persistence and real
P-256 signatures, and they assert what a device is *told*, never a flag: a
stale ref is corrected on the read, and is still refused on every write.
"""

from __future__ import annotations

import base64
import json
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
from hub.adapters.persistence.device_erase import SqlDeviceEraseLedger
from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.adapters.persistence.models import AdmissionClaimRow
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.device_control.application import AcceptDeviceManifest, PullDeviceConfiguration
from hub.device_control.http import DeviceEraseHttpServices, create_device_erase_router
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.management_events import DeviceManagementEventRecord

_DEVICE = named_device_instance_id("device_stale_ref")
_STRANGER = named_device_instance_id("device_never_enrolled")

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = "owner-domain_01"

#: What Admission holds after the Owner re-added an already-claimed Body.
LIVE = DeviceRef(
    device_instance_id=_DEVICE,
    owner_domain_id=OWNER,
    owner_domain_generation=1,
    claim_generation=4,
    trust_epoch=1,
)
#: What the device still has in flash, from the generation before that.
STALE = LIVE.model_copy(update={"claim_generation": 1})

MANIFEST = {
    "schema_version": 1,
    "actions": [],
    "events": [],
    "media": [{"codecs": ["opus"], "direction": "bidirectional", "kind": "audio"}],
    "properties": [],
    "title": "eidolon-mobile-body",
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


class _ChannelBinding:
    """Records the generation the Provider was asked to bind, and binds it."""

    def __init__(self) -> None:
        self.requested: list[DeviceRef] = []

    async def execute(self, *, device_ref: DeviceRef):
        self.requested.append(device_ref)
        return ()


class _Unused:
    def __getattr__(self, name):
        raise AssertionError(f"{name} is not part of this loop")


@pytest.fixture
async def hub(tmp_path):
    database = HubDatabase.sqlite(
        tmp_path / "hub.sqlite3",
        owner_domain_id=OWNER,
        owner_domain_generation=LIVE.owner_domain_generation,
    )
    await database.initialize_schema()
    try:
        yield database
    finally:
        await database.close()


async def _seed_live_claim(database, *, key, state: str = "active") -> None:
    """Persist the Claim and directory row exactly as Admission leaves them.

    Admission stores the operational key `p256-spki:`-prefixed and Device
    Control receives it bare, so seeding it the way Admission writes it is part
    of what these tests are checking.
    """

    repositories = SqlHubRepositories(database)
    await repositories.device_mutations.commit(
        expected=None,
        device=ManagedDevice(
            identity=DeviceIdentity(LIVE.device_instance_id),
            display_name="Phone",
            manifest_id="eidolon-mobile-body",
            manifest=DeviceManifestDocument.from_declaration(
                document=MANIFEST, declared_revision=3
            ),
            enrolled_at=NOW,
            updated_at=NOW,
            owner_domain_id=str(LIVE.owner_domain_id),
            owner_domain_generation=LIVE.owner_domain_generation,
            claim_generation=LIVE.claim_generation,
            trust_epoch=LIVE.trust_epoch,
            owner_id="owner_01",
            lifecycle_state=DeviceLifecycleState.APPROVED,
        ),
        event=DeviceManagementEventRecord(
            event_id="claim-activated_0004",
            event_type="live.eidolon.device.claim-activated.v1",
            source="urn:eidolon:authority:admission",
            principal_id=LIVE.device_instance_id,
            subject=LIVE.device_instance_id,
            occurred_at=NOW,
            data={"manifest_revision": manifest_digest(MANIFEST)},
        ),
    )
    async with database.sessions.begin() as session:
        session.add(
            AdmissionClaimRow(
                device_instance_id=LIVE.device_instance_id,
                owner_domain_id=str(LIVE.owner_domain_id),
                business_owner_id="owner_01",
                hardware_identity_ref="device-hw-" + "a1" * 32,
                owner_domain_generation=LIVE.owner_domain_generation,
                claim_generation=LIVE.claim_generation,
                trust_epoch=LIVE.trust_epoch,
                manifest_ref_json=json.dumps(
                    {
                        "manifest_id": "eidolon-mobile-body",
                        "revision": 3,
                        "digest": manifest_digest(MANIFEST),
                    }
                ),
                approval_decision_id="decision_04",
                operational_public_key_spki="p256-spki:" + _spki(key),
                state=state,
                revision=4,
                activated_at=NOW,
                updated_at=NOW,
                revoked_at=None,
            )
        )


def _app(database, *, key) -> tuple[FastAPI, _ChannelBinding]:
    repositories = SqlHubRepositories(database)
    claims = SqlDeviceEraseLedger(database)
    binding = _ChannelBinding()
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                configuration=PullDeviceConfiguration(claims=claims, devices=repositories.devices),
                manifest=AcceptDeviceManifest(
                    claims=claims,
                    devices=repositories.devices,
                    mutations=repositories.device_mutations,
                    directory=ProjectDeviceDirectory(
                        devices=repositories.devices,
                        directory=InMemoryDeviceDirectoryRepository(),
                    ),
                    ids=_Ids(),
                    clock=_Clock(),
                ),
                channel_binding=binding,
                pull=_Unused(),
                acknowledge=_Unused(),
                reconcile=_Unused(),
                ledger=_Unused(),
                secret=b"m" * 32,
            )
        )
    )
    return app, binding


def _pull(client, key, ref: DeviceRef, *, nonce: str):
    document = device_control_configuration_proof_document(device_ref=ref, nonce=nonce)
    return client.post(
        "/api/device-control/v1/configuration:pull",
        json={
            "device_ref": ref.model_dump(mode="json"),
            "nonce": nonce,
            "public_key_spki": _spki(key),
            "device_signature": _sign(key, document),
        },
    )


def _assert_manifest(client, key, ref: DeviceRef, *, revision: int):
    unsigned = AssertDeviceManifest(
        device_ref=ref,
        manifest=ManifestDocument(
            manifest_id="eidolon-mobile-body",
            revision=revision,
            digest=manifest_digest(MANIFEST),
            document=MANIFEST,
        ),
        nonce=f"manifest_nonce_{revision:04d}",
        public_key_spki=_spki(key),
        device_signature="A" * 86,
    )
    signed = unsigned.model_copy(
        update={"device_signature": _sign(key, unsigned.signing_document())}
    )
    return client.post(
        "/api/device-control/v1/manifest:assert", json=signed.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_a_stale_ref_is_answered_with_the_one_the_authority_holds(hub) -> None:
    """The whole incident, and its end: one call, and the device knows again."""

    key = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key)
    app, binding = _app(hub, key=key)

    with TestClient(app) as client:
        answer = _pull(client, key, STALE, nonce="stale_generation_pull_1")

        assert answer.status_code == 200, answer.text
        body = answer.json()
        # The correction itself: the device asked as generation 1 and is told
        # which generation it is actually at. Nothing else can tell it this.
        assert body["device_ref"] == LIVE.model_dump(mode="json")
        assert body["lifecycle_state"] == "approved"
        assert body["nonce"] == "stale_generation_pull_1"
        # And it is told what the Authority holds of its own declaration, so
        # the manifest repair loop is reachable from here too.
        assert body["manifest"] == {
            "manifest_id": "eidolon-mobile-body",
            "revision": 3,
            "digest": manifest_digest(MANIFEST),
        }

    # The Provider is asked for the live generation, never the stale one: the
    # Channel binding is keyed on the whole ref, so binding the ref the device
    # sent would provision a generation nobody holds.
    assert binding.requested == [LIVE]


@pytest.mark.asyncio
async def test_the_corrected_ref_is_the_one_that_works_on_the_next_call(hub) -> None:
    """A correction that does not settle is a loop, not a repair."""

    key = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key)
    app, binding = _app(hub, key=key)

    with TestClient(app) as client:
        corrected = DeviceRef.model_validate(
            _pull(client, key, STALE, nonce="stale_generation_pull_1").json()["device_ref"]
        )
        second = _pull(client, key, corrected, nonce="corrected_generation_pull")

    assert second.status_code == 200, second.text
    assert second.json()["device_ref"] == LIVE.model_dump(mode="json")
    assert binding.requested == [LIVE, LIVE]


@pytest.mark.asyncio
async def test_a_revoked_claim_still_says_revoked_to_a_device_at_any_generation(hub) -> None:
    """Removal is the one verdict a stale device must still hear.

    A Body carrying Owner data learns it has to drop it from this answer. If a
    stale ref were refused instead, the device most likely to be behind — the
    one that was off while the Owner removed it — is the one never told.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key, state="revoked")
    app, binding = _app(hub, key=key)

    with TestClient(app) as client:
        answer = _pull(client, key, STALE, nonce="revoked_stale_pull_1")

    assert answer.status_code == 200, answer.text
    assert answer.json()["lifecycle_state"] == "revoked"
    assert answer.json()["device_ref"] == LIVE.model_dump(mode="json")
    assert answer.json()["channels"] == []
    assert binding.requested == [], "a revoked Claim is never bound to a Channel"


@pytest.mark.asyncio
async def test_a_device_with_no_claim_here_is_still_refused(hub) -> None:
    """Correcting a ref is not the same as inventing one.

    The refusal has to survive, or the read would answer any caller that can
    sign for a key it chose itself.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key)
    app, _binding = _app(hub, key=key)
    stranger = STALE.model_copy(update={"device_instance_id": _STRANGER})

    with TestClient(app) as client:
        answer = _pull(client, key, stranger, nonce="unknown_device_pull_1")

    assert answer.status_code == 409
    assert answer.json()["detail"] == "STALE_GENERATION"


@pytest.mark.asyncio
async def test_another_owner_domain_is_still_refused(hub) -> None:
    """The Owner Domain is a boundary, not a generation, and is never corrected."""

    key = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key)
    app, _binding = _app(hub, key=key)
    elsewhere = STALE.model_copy(update={"owner_domain_id": "owner-domain_99"})

    with TestClient(app) as client:
        answer = _pull(client, key, elsewhere, nonce="other_owner_domain_pull")

    assert answer.status_code == 409
    assert answer.json()["detail"] == "STALE_GENERATION"


@pytest.mark.asyncio
async def test_another_key_is_still_refused_at_the_live_generation(hub) -> None:
    """Possession of the Claim's operational key is what authorizes this read.

    Relaxing the generation must not relax that, so it is checked against the
    key the Claim records rather than the key the request carries.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    impostor = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key)
    app, _binding = _app(hub, key=key)

    with TestClient(app) as client:
        answer = _pull(client, impostor, LIVE, nonce="impostor_key_pull_1")

    assert answer.status_code == 403
    assert answer.json()["detail"] == "device proof rejected"


@pytest.mark.asyncio
async def test_a_manifest_assertion_at_a_stale_ref_is_still_refused(hub) -> None:
    """The read is corrected; the write is not.

    A Manifest assertion changes what the Authority holds, and this surface
    keeps no record of the nonces it has already answered — so a signed
    assertion is replayable for as long as its ref matches. Ending that is what
    a generation is for. A device that is behind can learn its ref from
    `configuration:pull` and sign the assertion again; it never needs a stale
    write to be accepted.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    await _seed_live_claim(hub, key=key)
    app, _binding = _app(hub, key=key)

    with TestClient(app) as client:
        refused = _assert_manifest(client, key, STALE, revision=4)
        assert refused.status_code == 409
        assert refused.json()["detail"] == "CLAIM_NOT_ACTIVE"

        # And the same assertion at the ref the read handed back is accepted,
        # so the refusal above costs the device one extra call and nothing else.
        accepted = _assert_manifest(client, key, LIVE, revision=4)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["outcome"] == "accepted"
