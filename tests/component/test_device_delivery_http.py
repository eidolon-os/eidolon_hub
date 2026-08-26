from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from eidolon_sdk.device_foundation.v1 import (
    AdmissionCredential,
    BusinessOwnerId,
    ControllerActorRef,
    DeliverEnvelope,
    DeliveryAcceptance,
    DeviceEvidenceEnvelope,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceRef,
    canonical_bytes,
    issue_admission_credential,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.channel_reconciliation.domain import ChannelBinding
from hub.device_control.application import DeviceEraseDelivery, PullDeviceConfiguration
from hub.device_control.domain import DeviceEraseOperation, DeviceEraseState
from hub.device_control.http import DeviceEraseHttpServices, create_device_erase_router
from hub.device_control.ports import DeviceClaimProjection

NOW = datetime(2026, 8, 24, tzinfo=UTC)
REF = DeviceRef(
    device_instance_id="device_01",
    owner_domain_id="owner-domain_01",
    owner_domain_generation=3,
    claim_generation=7,
    trust_epoch=4,
)
COMMAND = DeviceLocalEraseCommand(
    operation_id="erase_operation_01",
    device_ref=REF,
    deadline=NOW + timedelta(days=7),
)


class _Reconcile:
    async def execute(self):
        return 0


class _Pull:
    async def execute(self, **_kwargs):
        return DeviceEraseDelivery(
            delivery_attempt_id="erase_attempt_01",
            command=COMMAND,
        )


class _Configuration:
    async def execute(self, **_kwargs):
        raise AssertionError("configuration was not requested")


def _directory_device():
    from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
    from hub.domain.devices.identity import DeviceIdentity
    from hub.domain.devices.manifest import DeviceManifestDocument

    return ManagedDevice(
        identity=DeviceIdentity(REF.device_instance_id),
        display_name="Box",
        device_kind="esp-box-3",
        manifest=DeviceManifestDocument.from_declaration(
            document={"schema_version": 1, "media": [{"kind": "audio"}]},
            declared_revision=4,
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


class _Devices:
    """The directory row a configuration answer reports the accepted Manifest from."""

    def __init__(self, device=None) -> None:
        self.device = device

    async def get(self, device_id: str):
        if self.device is None or device_id != self.device.identity.device_id:
            return None
        return self.device


class _Manifest:
    """Manifest assertion is a separate surface; these tests do not exercise it."""

    def __init__(self, acceptance=None) -> None:
        self.acceptance = acceptance
        self.asserted = []

    async def execute(self, *, assertion):
        if self.acceptance is None:
            raise AssertionError("manifest assertion was not requested")
        self.asserted.append(assertion)
        return self.acceptance


class _ChannelBinding:
    def __init__(self, channels=()) -> None:
        self.channels = channels
        self.requested = []

    async def execute(self, *, device_ref):
        self.requested.append(device_ref)
        return self.channels


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
    der = key.sign(canonical_bytes(document), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


class _ClaimReader:
    def __init__(self, projection: DeviceClaimProjection) -> None:
        self.projection = projection

    async def get_exact(self, *, device_ref):
        return self.projection if device_ref == self.projection.device_ref else None


def test_configuration_pull_reconciles_provider_binding_after_active_claim() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    public_key_spki = _spki(key)
    nonce = "fresh_nonce_000001"
    document = {
        "device_ref": REF.model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-control.configuration",
    }
    binding = _ChannelBinding(
        (
            ChannelBinding(
                channel_id="channel_01",
                purpose="device-session",
                kinds=("audio",),
                binding_format="application/vnd.eidolon.livekit-session+json;v=2",
                issued_at_ms=1,
                expires_at_ms=1_800_000_000_000,
                opaque_binding="e30=",
            ),
        )
    )
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=_Manifest(),
                configuration=PullDeviceConfiguration(
                    claims=_ClaimReader(
                        DeviceClaimProjection(
                            device_ref=REF,
                            state="active",
                            operational_public_key_spki=public_key_spki,
                        )
                    ),
                    devices=_Devices(_directory_device()),
                ),
                channel_binding=binding,
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=b"m" * 32,
            )
        )
    )
    response = TestClient(app).post(
        "/api/device-control/v1/configuration:pull",
        json={
            "device_ref": REF.model_dump(mode="json"),
            "nonce": nonce,
            "public_key_spki": public_key_spki,
            "device_signature": _sign(key, document),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "operation": "device-control.configuration",
        "nonce": nonce,
        "device_ref": REF.model_dump(mode="json"),
        "lifecycle_state": "approved",
        # A device learns which of its own declarations the Authority holds from
        # the same answer that tells it where to connect.
        "manifest": {
            "manifest_id": "esp-box-3",
            "revision": 4,
            "digest": _directory_device().manifest_digest,
        },
        "channels": [
            {
                "channel_id": "channel_01",
                "purpose": "device-session",
                "kinds": ["audio"],
                "binding_format": "application/vnd.eidolon.livekit-session+json;v=2",
                "issued_at_ms": 1,
                "expires_at_ms": 1_800_000_000_000,
                "opaque_binding": "e30=",
            }
        ],
    }
    assert binding.requested == [REF]


class _Ack:
    async def execute(self, *, ack):
        assert ack.operation_id == COMMAND.operation_id
        return DeviceEraseOperation(
            source_event_id="event_01",
            command=COMMAND,
            request_fingerprint="sha256:" + "a" * 64,
            public_key_spki="key",
            key_id=None,
            state=DeviceEraseState.ACKNOWLEDGED,
            created_at=NOW,
            attempt_count=1,
            delivery_attempt_id="erase_attempt_01",
            acknowledged_at=NOW,
            terminal_result="erased",
        )


class _RecordingAck(_Ack):
    called = False

    async def execute(self, *, ack):
        self.called = True
        return await super().execute(ack=ack)


class _Ledger:
    async def get(self, *, operation_id):
        assert operation_id == COMMAND.operation_id
        return await _Ack().execute(
            ack=DeviceLocalEraseAck(
                operation_id=COMMAND.operation_id,
                device_ref=REF,
                ack_sequence=1,
                result="erased",
                result_code="ERASED",
                device_monotonic_time=1234,
                device_signature="A" * 86,
            )
        )

    async def get_by_source_event(self, *, source_claim_event_id, device_ref):
        assert source_claim_event_id == "admission-event_01"
        assert device_ref == REF
        return await self.get(operation_id=COMMAND.operation_id)


def test_the_key_a_device_presents_is_the_key_its_claim_recorded() -> None:
    """One key, two contracts, two spellings — compared as a key, not a string.

    Admission records `p256-spki:<base64>`; Device Control's schema forbids the
    colon and receives `<base64>`. A plain `!=` between them could never be
    false for a correct device, so every claimed device was refused on the first
    call it makes after a reboot, and neither spelling could have passed.

    Every fixture here stored the Device Control spelling, which is why the
    Authority's own tests agreed with it and a real Host did not.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    presented = _spki(key)
    nonce = "fresh_nonce_000003"
    document = {
        "device_ref": REF.model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-control.configuration",
    }
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=_Manifest(),
                configuration=PullDeviceConfiguration(
                    claims=_ClaimReader(
                        DeviceClaimProjection(
                            device_ref=REF,
                            state="active",
                            # As Admission wrote it.
                            operational_public_key_spki="p256-spki:" + presented,
                        )
                    ),
                    devices=_Devices(),
                ),
                channel_binding=_ChannelBinding(),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=b"m" * 32,
            )
        )
    )
    client = TestClient(app)
    accepted = client.post(
        "/api/device-control/v1/configuration:pull",
        json={
            "device_ref": REF.model_dump(mode="json"),
            "nonce": nonce,
            "public_key_spki": presented,
            "device_signature": _sign(key, document),
        },
    )
    other = ec.generate_private_key(ec.SECP256R1())
    refused = client.post(
        "/api/device-control/v1/configuration:pull",
        json={
            "device_ref": REF.model_dump(mode="json"),
            "nonce": nonce,
            "public_key_spki": _spki(other),
            "device_signature": _sign(other, document),
        },
    )

    assert accepted.status_code == 200
    assert accepted.json()["lifecycle_state"] == "approved"
    # Another key is still another key.
    assert refused.status_code == 403


_SECRET = b"admission-owner-secret-value-0001"


def _erase_app() -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=_Manifest(),
                configuration=_Configuration(),
                channel_binding=_ChannelBinding(),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=_SECRET,
            )
        )
    )
    return app


def _removal_credential(**overrides) -> str:
    """The credential Admin mints for a removal, minted the same way here."""

    values = {
        "subject": "eidolon-admin/admission-consumer",
        "actor": ControllerActorRef(
            principal_id="ectrl-0123456789abcdefabcd",
            owner_domain_id=REF.owner_domain_id,
            granted_scopes=("device.claim.revoke",),
            authentication_strength="software",
        ),
        "owner_domain_id": REF.owner_domain_id,
        "business_owner_id": BusinessOwnerId("owner_683f963f54885e868924"),
        "scopes": ("device.claim.revoke",),
        "target_device_ref": REF,
    }
    values.update(overrides)
    return issue_admission_credential(
        AdmissionCredential(**values), secret=_SECRET, ttl_seconds=60
    )


def test_status_lookup_binds_source_event_and_full_device_generation() -> None:
    response = TestClient(_erase_app()).get(
        f"/api/device-control/v1/owners/{REF.owner_domain_id}/devices/"
        f"{REF.device_instance_id}/erase-operations",
        params={
            "source_claim_event_id": "admission-event_01",
            "owner_domain_generation": REF.owner_domain_generation,
            "claim_generation": REF.claim_generation,
            "trust_epoch": REF.trust_epoch,
        },
        headers={"Authorization": _removal_credential()},
    )

    assert response.status_code == 200
    status = response.json()
    assert status["operation_id"] == COMMAND.operation_id
    assert status["device_ref"] == REF.model_dump(mode="json")


def test_https_delivery_adapter_uses_only_canonical_envelopes() -> None:
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=_Manifest(),
                configuration=_Configuration(),
                channel_binding=_ChannelBinding(),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=b"m" * 32,
            )
        )
    )
    client = TestClient(app)
    pulled = client.post(
        "/api/device-control/v1/erase-operations:pull",
        json={
            "device_ref": REF.model_dump(mode="json"),
            "nonce": "fresh_nonce_000001",
            "public_key_spki": "K" * 120,
            "device_signature": "A" * 86,
        },
    )
    assert pulled.status_code == 200
    deliver = DeliverEnvelope.model_validate(pulled.json())
    assert deliver.message_id == COMMAND.operation_id
    assert deliver.kind == "operation"

    ack = DeviceLocalEraseAck(
        operation_id=COMMAND.operation_id,
        device_ref=REF,
        ack_sequence=1,
        result="erased",
        result_code="ERASED",
        device_monotonic_time=1234,
        device_signature="A" * 86,
    )
    evidence = DeviceEvidenceEnvelope(
        delivery_attempt_id=deliver.delivery_attempt_id,
        message_id=deliver.message_id,
        device_ref=REF,
        payload_schema=(
            "https://contracts.eidolon.live/device-foundation/v1/device-control/"
            "schemas.schema.json#/$defs/DeviceLocalEraseAck"
        ),
        payload=ack.model_dump(mode="json"),
    )
    accepted = client.post(
        f"/api/device-control/v1/erase-operations/{COMMAND.operation_id}/ack",
        json=evidence.model_dump(mode="json"),
    )
    assert accepted.status_code == 200
    assert DeliveryAcceptance.model_validate(accepted.json()).state == "accepted"


def test_nothing_to_deliver_is_an_empty_204_not_a_failure() -> None:
    """The normal answer for a healthy claimed device.

    A device polls this on every boot, and almost always there is nothing
    waiting for it. Declaring a response model made that answer unrepresentable
    — FastAPI validated the absent body against the envelope and answered 500 —
    so a claimed BOX-3 could not finish booting at all: it retried, backed off,
    and never reached its runtime.
    """

    class _NothingPending:
        async def execute(self, **_kwargs):
            return None

    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=_Manifest(),
                configuration=_Configuration(),
                channel_binding=_ChannelBinding(),
                pull=_NothingPending(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=b"m" * 32,
            )
        )
    )
    pulled = TestClient(app).post(
        "/api/device-control/v1/erase-operations:pull",
        json={
            "device_ref": REF.model_dump(mode="json"),
            "nonce": "fresh_nonce_000002",
            "public_key_spki": "K" * 120,
            "device_signature": "A" * 86,
        },
    )

    assert pulled.status_code == 204
    assert pulled.content == b""


def test_ack_rejects_wrong_delivery_attempt_before_applying_evidence() -> None:
    acknowledge = _RecordingAck()
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=_Manifest(),
                configuration=_Configuration(),
                channel_binding=_ChannelBinding(),
                pull=_Pull(),
                acknowledge=acknowledge,
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=b"m" * 32,
            )
        )
    )
    ack = DeviceLocalEraseAck(
        operation_id=COMMAND.operation_id,
        device_ref=REF,
        ack_sequence=1,
        result="erased",
        result_code="ERASED",
        device_monotonic_time=1234,
        device_signature="A" * 86,
    )
    evidence = DeviceEvidenceEnvelope(
        delivery_attempt_id="erase_attempt_stale",
        message_id=COMMAND.operation_id,
        device_ref=REF,
        payload_schema=(
            "https://contracts.eidolon.live/device-foundation/v1/device-control/"
            "schemas.schema.json#/$defs/DeviceLocalEraseAck"
        ),
        payload=ack.model_dump(mode="json"),
    )

    response = TestClient(app).post(
        f"/api/device-control/v1/erase-operations/{COMMAND.operation_id}/ack",
        json=evidence.model_dump(mode="json"),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "STALE_GENERATION"
    assert acknowledge.called is False


def _manifest_document(*, camera: bool) -> dict[str, object]:
    from eidolon_sdk.device_foundation.v1 import ManifestDocument, manifest_digest

    document = {
        "schema_version": 1,
        "media": [{"kind": "audio"}] + ([{"kind": "video"}] if camera else []),
    }
    return ManifestDocument(
        manifest_id="esp-box-3",
        revision=2 if camera else 1,
        digest=manifest_digest(document),
        document=document,
    )


def test_manifest_assertion_is_refused_unless_the_claim_key_signed_it() -> None:
    """The route exists so a claimed device can correct its own declaration.

    It is on the device-authenticated surface, so the only thing that makes an
    assertion this device's assertion is the signature its Claim recorded.
    """

    from datetime import datetime as _datetime

    from eidolon_sdk.device_foundation.v1 import (
        AssertDeviceManifest,
        DeviceManifestAcceptance,
    )

    key = ec.generate_private_key(ec.SECP256R1())
    stranger = ec.generate_private_key(ec.SECP256R1())
    manifest = _manifest_document(camera=True)
    accepted = DeviceManifestAcceptance(
        device_ref=REF,
        nonce="manifest_nonce_00001",
        accepted=manifest.ref,
        outcome="accepted",
        accepted_at=_datetime(2026, 8, 25, tzinfo=UTC),
    )

    class _Accepting:
        def __init__(self) -> None:
            self.asserted = []

        async def execute(self, *, assertion):
            # The router must hand over exactly what the device signed.
            verify = {
                "public_key_spki": assertion.public_key_spki,
                "signing_document": assertion.signing_document(),
                "signature": assertion.device_signature,
            }
            from eidolon_sdk.device_foundation.v1 import verify_p256_signature

            verify_p256_signature(**verify)
            self.asserted.append(assertion)
            return accepted

    service = _Accepting()
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                manifest=service,
                configuration=_Configuration(),
                channel_binding=_ChannelBinding(),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                secret=b"m" * 32,
            )
        )
    )

    def _body(signer: ec.EllipticCurvePrivateKey) -> dict:
        unsigned = AssertDeviceManifest(
            device_ref=REF,
            manifest=manifest,
            nonce="manifest_nonce_00001",
            public_key_spki=_spki(signer),
            device_signature="A" * 86,
        )
        signed = unsigned.model_copy(
            update={"device_signature": _sign(signer, unsigned.signing_document())}
        )
        return signed.model_dump(mode="json")

    with TestClient(app) as client:
        response = client.post("/api/device-control/v1/manifest:assert", json=_body(key))
        assert response.status_code == 200
        assert response.json()["outcome"] == "accepted"
        assert response.json()["accepted"]["revision"] == 2

        forged = _body(key)
        forged["public_key_spki"] = _spki(stranger)
        assert client.post("/api/device-control/v1/manifest:assert", json=forged).status_code == 403

        # A document whose digest does not describe it is not a Manifest at all,
        # and is refused by the contract before any use case sees it.
        tampered = _body(key)
        tampered["manifest"]["document"]["media"] = []
        assert (
            client.post("/api/device-control/v1/manifest:assert", json=tampered).status_code == 422
        )

    assert len(service.asserted) == 1


def _erase_status(credential: str):
    return TestClient(_erase_app()).get(
        f"/api/device-control/v1/owners/{REF.owner_domain_id}/devices/"
        f"{REF.device_instance_id}/erase-operations",
        params={
            "source_claim_event_id": "admission-event_01",
            "owner_domain_generation": REF.owner_domain_generation,
            "claim_generation": REF.claim_generation,
            "trust_epoch": REF.trust_epoch,
        },
        headers={"Authorization": credential},
    )


def test_a_credential_for_another_generation_cannot_read_this_one() -> None:
    """What the fencing is for: a credential names one exact incarnation."""

    older = REF.model_copy(update={"claim_generation": REF.claim_generation - 1})

    refused = _erase_status(_removal_credential(target_device_ref=older))

    assert refused.status_code == 403


def test_a_credential_that_names_no_device_authorizes_none() -> None:
    """"Not fenced" is not "fenced to whatever arrived"."""

    refused = _erase_status(_removal_credential(target_device_ref=None))

    assert refused.status_code == 403


def test_a_credential_without_the_revoke_scope_cannot_read_the_erase_state() -> None:
    reading_only = ControllerActorRef(
        principal_id="ectrl-0123456789abcdefabcd",
        owner_domain_id=REF.owner_domain_id,
        granted_scopes=("device.read",),
        authentication_strength="software",
    )

    refused = _erase_status(
        _removal_credential(actor=reading_only, scopes=("device.read",))
    )

    assert refused.status_code == 403


def test_the_other_surface_s_vocabulary_is_refused_here_too() -> None:
    """The shape that answered 401 for every removal, refused by name."""

    import jwt

    token = jwt.encode(
        {
            "sub": "eidolon-admin/lifecycle-workflow",
            "presenter": "eidolon-admin/lifecycle-workflow",
            "aud": "eidolon-admission",
            "actor_ref": "controller:ectrl-0123456789abcdefabcd",
            "owner_id": str(REF.owner_domain_id),
            "roles": ["device-manager"],
            "scopes": ["device.claim.revoke"],
            "target_device_id": REF.device_instance_id,
            "exp": 4_000_000_000,
        },
        _SECRET,
        algorithm="HS256",
    )

    assert _erase_status(f"Bearer {token}").status_code == 403
