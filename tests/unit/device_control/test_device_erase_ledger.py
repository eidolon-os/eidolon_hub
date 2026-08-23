from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from eidolon_sdk.device_foundation.v1 import (
    DeviceLocalEraseAck,
    DeviceOperationKeyProof,
    DeviceRef,
    canonical_bytes,
    operation_key_id,
)
from sqlalchemy import select

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.device_erase import SqlDeviceEraseLedger
from hub.adapters.persistence.models import ClaimEventRow
from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher
from hub.device_control.application import (
    AcknowledgeDeviceEraseOperation,
    BindDeviceOperationKey,
    PullDeviceEraseOperation,
    ReconcileDeviceEraseOperations,
)
from hub.device_control.domain import (
    DeviceEraseGenerationConflict,
    DeviceEraseIdempotencyConflict,
    DeviceEraseState,
)
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
MANIFEST = "sha256:" + "a" * 64
TOKEN = "device-generated-random-token-000001"


class Clock:
    def __init__(self, now: datetime = NOW):
        self.value = now

    def now(self) -> datetime:
        return self.value


class EnrollmentDeviceRepository:
    def __init__(self, device: ManagedDevice):
        self.device = device

    async def get_by_enrollment_id(self, enrollment_id: str) -> ManagedDevice | None:
        return self.device if enrollment_id == self.device.enrollment_id else None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _spki(key: ec.EllipticCurvePrivateKey) -> str:
    return _b64(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def _sign(key: ec.EllipticCurvePrivateKey, document: dict[str, object]) -> str:
    der = key.sign(canonical_bytes(document), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _ref(generation: int = 7) -> DeviceRef:
    return DeviceRef(
        device_instance_id="device_erase_01",
        owner_domain_id="owner_01",
        claim_generation=generation,
        trust_epoch=4,
        accepted_manifest_digest=MANIFEST,
    )


async def _seed_event(
    database: HubDatabase,
    *,
    generation: int = 7,
    event_id: str | None = None,
    occurred_at: datetime = NOW,
) -> str:
    event_id = event_id or f"claim_event_{generation}"
    async with database.sessions.begin() as session:
        session.add(
            ClaimEventRow(
                event_id=event_id,
                event_type="live.eidolon.device.claim-revoked.v1",
                device_id="device_erase_01",
                owner_domain_id="owner_01",
                claim_generation=generation,
                trust_epoch=4,
                accepted_manifest_digest=MANIFEST,
                aggregate_revision=generation + 1,
                correlation_id=f"removal_intent_{generation}",
                causation_id=f"revoke_claim_{generation}",
                actor_principal_id="controller_01",
                occurred_at=occurred_at,
                reason="owner-removed",
            )
        )
    return event_id


async def _bind(
    ledger: SqlDeviceEraseLedger,
    key: ec.EllipticCurvePrivateKey,
    *,
    generation: int = 7,
) -> None:
    proof = DeviceOperationKeyProof(
        device_instance_id="device_erase_01",
        enrollment_request_id=f"enrollment_request_{generation}",
        public_key_spki=_spki(key),
        possession_signature="A" * 86,
    )
    await ledger.bind_operation_key(
        enrollment_id=f"enrollment_{generation}",
        claim_generation=generation,
        proof=proof,
        key_id=operation_key_id(proof.public_key_spki),
        bound_at=NOW,
    )


@pytest.fixture
async def database(tmp_path):
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await database.initialize_schema()
    try:
        yield database
    finally:
        await database.close()


async def test_device_erase_operation_online_ack_and_signature_verification(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger = SqlDeviceEraseLedger(database)
    await _bind(ledger, key)
    await _seed_event(database)
    clock = Clock()
    reconcile = ReconcileDeviceEraseOperations(
        ledger=ledger, clock=clock, operation_ttl=timedelta(days=7)
    )
    assert await reconcile.execute() == 1

    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None
    assert operation.state is DeviceEraseState.PENDING
    original_operation_id = operation.command.operation_id

    pull_document = {
        "device_ref": _ref().model_dump(mode="json"),
        "nonce": "fresh_nonce_000001",
        "operation_type": "device-local.erase",
    }
    delivery = await PullDeviceEraseOperation(ledger=ledger, clock=clock).execute(
        device_ref=_ref(),
        public_key_spki=_spki(key),
        nonce="fresh_nonce_000001",
        signature=_sign(key, pull_document),
    )
    assert delivery is not None
    assert delivery.command.operation_id == original_operation_id
    accepted = await ledger.get(operation_id=original_operation_id)
    assert accepted is not None
    assert accepted.state is DeviceEraseState.DELIVERY_ACCEPTED
    assert accepted.attempt_count == 1

    ack_values = {
        "contract": "eidolon.device-foundation.device-operation-ack",
        "contract_version": "1.0",
        "operation_id": original_operation_id,
        "operation_type": "device-local.erase",
        "device_ref": _ref().model_dump(mode="json"),
        "ack_sequence": 1,
        "result": "erased",
        "result_code": "ERASED",
        "device_monotonic_time": 1234,
    }
    ack = DeviceLocalEraseAck(
        **ack_values,
        device_signature=_sign(key, ack_values),
    )
    terminal = await AcknowledgeDeviceEraseOperation(
        ledger=ledger, clock=clock
    ).execute(ack=ack)
    assert terminal.state is DeviceEraseState.ACKNOWLEDGED
    assert terminal.terminal_result == "erased"

    wrong_key = ec.generate_private_key(ec.SECP256R1())
    forged = ack.model_copy(
        update={"ack_sequence": 2, "device_signature": _sign(wrong_key, ack.signing_document())}
    )
    with pytest.raises(ValueError, match="signature"):
        await AcknowledgeDeviceEraseOperation(ledger=ledger, clock=clock).execute(ack=forged)


async def test_enrollment_binds_signed_operation_key_to_claim_generation(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    tokens = Sha256RetrievalTokenHasher()
    device = ManagedDevice(
        identity=DeviceIdentity("device_erase_01"),
        enrollment_id="enrollment_7",
        retrieval_token_hash=tokens.hash(TOKEN),
        retrieval_expires_at=NOW + timedelta(minutes=30),
        display_name="Erase device",
        device_kind="simulator",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Erase device"}
        ),
        enrolled_at=NOW,
        updated_at=NOW,
        last_enrollment_request_id="enrollment_request_7",
        claim_generation=7,
    )
    proof_values = {
        "device_instance_id": device.identity.device_id,
        "enrollment_request_id": device.last_enrollment_request_id,
        "public_key_spki": _spki(key),
    }
    proof = DeviceOperationKeyProof(
        **proof_values,
        possession_signature=_sign(key, proof_values),
    )
    ledger = SqlDeviceEraseLedger(database)
    key_id = await BindDeviceOperationKey(
        devices=EnrollmentDeviceRepository(device),
        tokens=tokens,
        ledger=ledger,
        clock=Clock(),
    ).execute(
        enrollment_id=device.enrollment_id,
        retrieval_token=TOKEN,
        proof=proof,
    )
    assert key_id == operation_key_id(proof.public_key_spki)

    await _seed_event(database)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(days=7))
    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None
    assert operation.key_id == key_id
    assert operation.public_key_spki == proof.public_key_spki


async def test_offline_restart_and_host_relocation_keep_original_operation_id(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    first = SqlDeviceEraseLedger(database)
    await _bind(first, key)
    await _seed_event(database)
    await first.materialize_claim_events(now=NOW, operation_ttl=timedelta(days=7))
    pending = await first.get_for_device(device_ref=_ref())
    assert pending is not None and pending.state is DeviceEraseState.ACCEPTED

    relocated = SqlDeviceEraseLedger(database)
    assert await relocated.materialize_claim_events(
        now=NOW + timedelta(hours=1), operation_ttl=timedelta(days=7)
    ) == 0
    recovered = await relocated.get_for_device(device_ref=_ref())
    assert recovered is not None
    assert recovered.command.operation_id == pending.command.operation_id
    assert recovered.command.deadline == pending.command.deadline


async def test_deadline_and_late_ack_do_not_rewrite_expired_terminal(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger = SqlDeviceEraseLedger(database)
    await _bind(ledger, key)
    await _seed_event(database)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(minutes=5))
    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None
    clock = Clock(NOW + timedelta(minutes=6))
    assert await ledger.expire_due(now=clock.now()) == 1

    ack_values = {
        "contract": "eidolon.device-foundation.device-operation-ack",
        "contract_version": "1.0",
        "operation_id": operation.command.operation_id,
        "operation_type": "device-local.erase",
        "device_ref": _ref().model_dump(mode="json"),
        "ack_sequence": 1,
        "result": "erased",
        "result_code": "ERASED",
        "device_monotonic_time": 9999,
    }
    late = DeviceLocalEraseAck(**ack_values, device_signature=_sign(key, ack_values))
    unchanged = await AcknowledgeDeviceEraseOperation(
        ledger=ledger, clock=clock
    ).execute(ack=late)
    assert unchanged.state is DeviceEraseState.EXPIRED
    assert unchanged.terminal_result == "deadline-expired"


async def test_pull_crossing_deadline_never_delivers_expired_operation(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger = SqlDeviceEraseLedger(database)
    await _bind(ledger, key)
    await _seed_event(database)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(minutes=5))
    pull_document = {
        "device_ref": _ref().model_dump(mode="json"),
        "nonce": "fresh_nonce_000002",
        "operation_type": "device-local.erase",
    }
    delivery = await PullDeviceEraseOperation(
        ledger=ledger, clock=Clock(NOW + timedelta(minutes=5))
    ).execute(
        device_ref=_ref(),
        public_key_spki=_spki(key),
        nonce="fresh_nonce_000002",
        signature=_sign(key, pull_document),
    )
    assert delivery is None
    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None and operation.state is DeviceEraseState.EXPIRED


async def test_permanent_failure_and_duplicate_ack_conflict(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger = SqlDeviceEraseLedger(database)
    await _bind(ledger, key)
    await _seed_event(database)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(days=7))
    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None
    values = {
        "contract": "eidolon.device-foundation.device-operation-ack",
        "contract_version": "1.0",
        "operation_id": operation.command.operation_id,
        "operation_type": "device-local.erase",
        "device_ref": _ref().model_dump(mode="json"),
        "ack_sequence": 1,
        "result": "permanent-failure",
        "result_code": "LOCAL_STORE_DAMAGED",
        "device_monotonic_time": 1234,
    }
    ack = DeviceLocalEraseAck(**values, device_signature=_sign(key, values))
    service = AcknowledgeDeviceEraseOperation(ledger=ledger, clock=Clock())
    terminal = await service.execute(ack=ack)
    assert terminal.state is DeviceEraseState.PERMANENT_FAILURE
    assert (await service.execute(ack=ack)).state is DeviceEraseState.PERMANENT_FAILURE

    different_values = {**values, "result_code": "KEYSTORE_UNAVAILABLE"}
    different = DeviceLocalEraseAck(
        **different_values,
        device_signature=_sign(key, different_values),
    )
    with pytest.raises(DeviceEraseIdempotencyConflict):
        await service.execute(ack=different)


async def test_old_generation_ack_cannot_change_new_claim(database) -> None:
    key7 = ec.generate_private_key(ec.SECP256R1())
    key8 = ec.generate_private_key(ec.SECP256R1())
    ledger = SqlDeviceEraseLedger(database)
    await _bind(ledger, key7, generation=7)
    await _bind(ledger, key8, generation=8)
    await _seed_event(database, generation=7)
    await _seed_event(database, generation=8)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(days=7))
    current = await ledger.get_for_device(device_ref=_ref(8))
    assert current is not None
    values = {
        "contract": "eidolon.device-foundation.device-operation-ack",
        "contract_version": "1.0",
        "operation_id": current.command.operation_id,
        "operation_type": "device-local.erase",
        "device_ref": _ref(7).model_dump(mode="json"),
        "ack_sequence": 1,
        "result": "erased",
        "result_code": "ERASED",
        "device_monotonic_time": 1234,
    }
    stale = DeviceLocalEraseAck(**values, device_signature=_sign(key7, values))
    with pytest.raises(DeviceEraseGenerationConflict):
        await AcknowledgeDeviceEraseOperation(ledger=ledger, clock=Clock()).execute(
            ack=stale
        )
    unchanged = await ledger.get(operation_id=current.command.operation_id)
    assert unchanged is not None and unchanged.state is DeviceEraseState.ACCEPTED


async def test_missing_bound_key_is_explicit_permanent_failure(database) -> None:
    ledger = SqlDeviceEraseLedger(database)
    await _seed_event(database)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(days=7))
    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None
    assert operation.state is DeviceEraseState.PERMANENT_FAILURE
    assert operation.result_code == "ACK_KEY_NOT_BOUND"


async def test_same_operation_id_with_changed_deadline_is_an_idempotency_conflict(
    database,
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger = SqlDeviceEraseLedger(database)
    await _bind(ledger, key)
    await _seed_event(database)
    await ledger.materialize_claim_events(now=NOW, operation_ttl=timedelta(days=7))
    async with database.sessions.begin() as session:
        event = await session.scalar(
            select(ClaimEventRow).where(ClaimEventRow.event_id == "claim_event_7")
        )
        assert event is not None
        event.occurred_at = NOW + timedelta(minutes=1)
    with pytest.raises(DeviceEraseIdempotencyConflict):
        await ledger.materialize_claim_events(
            now=NOW + timedelta(minutes=1), operation_ttl=timedelta(days=7)
        )
