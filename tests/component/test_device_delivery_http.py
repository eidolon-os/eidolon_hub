from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eidolon_sdk.device_foundation.v1 import (
    DeliverEnvelope,
    DeliveryAcceptance,
    DeviceEvidenceEnvelope,
    DeviceLocalEraseAck,
    DeviceLocalEraseCommand,
    DeviceRef,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.device_control.application import DeviceEraseDelivery
from hub.device_control.domain import DeviceEraseOperation, DeviceEraseState
from hub.device_control.http import DeviceEraseHttpServices, create_device_erase_router

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


def test_https_delivery_adapter_uses_only_canonical_envelopes() -> None:
    app = FastAPI()
    app.include_router(
        create_device_erase_router(
            DeviceEraseHttpServices(
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
