from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from eidolon_sdk.device_foundation.v1 import (
    DeliverEnvelope,
    DeliveryAcceptance,
    DeviceEvidenceEnvelope,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceRef,
    canonical_bytes,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.device_control.application import DeviceEraseDelivery, PullDeviceConfiguration
from hub.device_control.domain import DeviceEraseOperation, DeviceEraseState
from hub.device_control.http import DeviceEraseHttpServices, create_device_erase_router
from hub.device_control.ports import DeviceClaimProjection
from hub.ports.identity import ManagementPrincipal

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


def test_configuration_pull_is_exact_claim_projection_without_channel_provision() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    public_key_spki = _spki(key)
    nonce = "fresh_nonce_000001"
    document = {
        "device_ref": REF.model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-control.configuration",
    }
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                configuration=PullDeviceConfiguration(
                    claims=_ClaimReader(
                        DeviceClaimProjection(
                            device_ref=REF,
                            state="active",
                            operational_public_key_spki=public_key_spki,
                        )
                    )
                ),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                authorizer=object(),
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
        "channels": [],
    }


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


class _Authorizer:
    async def authorize(self, *, credential, permission, owner_scope, device_id):
        assert credential == "Bearer exact-removal"
        assert owner_scope == str(REF.owner_domain_id)
        assert device_id == REF.device_instance_id
        return ManagementPrincipal(
            subject_id="eidolon-admin/lifecycle-workflow",
            owner_id=str(REF.owner_domain_id),
            roles=frozenset({"device-manager"}),
            scopes=frozenset({"device.claim.revoke"}),
            target_device_id=REF.device_instance_id,
            target_owner_domain_generation=REF.owner_domain_generation,
            target_claim_generation=REF.claim_generation,
            target_trust_epoch=REF.trust_epoch,
        )


def test_status_lookup_binds_source_event_and_full_device_generation() -> None:
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                configuration=_Configuration(),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                authorizer=_Authorizer(),
            )
        )
    )
    response = TestClient(app).get(
        f"/api/device-control/v1/owners/{REF.owner_domain_id}/devices/"
        f"{REF.device_instance_id}/erase-operations",
        params={
            "source_claim_event_id": "admission-event_01",
            "owner_domain_generation": REF.owner_domain_generation,
            "claim_generation": REF.claim_generation,
            "trust_epoch": REF.trust_epoch,
        },
        headers={"Authorization": "Bearer exact-removal"},
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
                configuration=_Configuration(),
                pull=_Pull(),
                acknowledge=_Ack(),
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                authorizer=object(),
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


def test_ack_rejects_wrong_delivery_attempt_before_applying_evidence() -> None:
    acknowledge = _RecordingAck()
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
                configuration=_Configuration(),
                pull=_Pull(),
                acknowledge=acknowledge,
                reconcile=_Reconcile(),
                ledger=_Ledger(),
                authorizer=object(),
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
