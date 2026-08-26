from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from eidolon_sdk.device_foundation.v1 import DeviceLocalEraseAck, DeviceRef, canonical_bytes
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.device_erase import SqlDeviceEraseLedger
from hub.adapters.persistence.models import AdmissionClaimEventStreamRow, AdmissionClaimRow
from hub.device_control.application import (
    AcknowledgeDeviceEraseOperation,
    PullDeviceEraseOperation,
    ReconcileDeviceEraseOperations,
)
from hub.device_control.domain import (
    DeviceEraseGenerationConflict,
    DeviceEraseIdempotencyConflict,
    DeviceEraseState,
)

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_ERASE_01 = named_device_instance_id("device_erase_01")

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
MANIFEST = "sha256:" + "a" * 64


class Clock:
    def __init__(self, now: datetime = NOW):
        self.value = now

    def now(self) -> datetime:
        return self.value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


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
        device_instance_id=_DEVICE_ERASE_01,
        owner_domain_id="owner-domain_01",
        owner_domain_generation=1,
        claim_generation=generation,
        trust_epoch=4,
    )


async def _seed_revoke(database, key, *, occurred_at=NOW) -> None:
    device_ref = _ref()
    event = {
        "specversion": "1.0",
        "id": "claim_event_7",
        "source": "urn:eidolon:authority:admission",
        "type": "live.eidolon.device.claim-revoked.v1",
        "subject": f"device-instances/{_DEVICE_ERASE_01}",
        "time": occurred_at.isoformat().replace("+00:00", "Z"),
        "datacontenttype": "application/json",
        "dataschema": "https://contracts.eidolon.live/device-foundation/v1/events/claim-revoked-data.schema.json",
        "audience": "eidolon-claim-consumers",
        "ownerdomainid": "owner-domain_01",
        "aggregaterev": 8,
        "correlationid": "removal_intent_7",
        "causationid": "revoke_claim_7",
        "data": {
            "device_ref": device_ref.model_dump(mode="json"),
            "reason": "owner-removed",
            "revoked_at": occurred_at.isoformat().replace("+00:00", "Z"),
        },
    }
    async with database.sessions.begin() as session:
        session.add(
            AdmissionClaimRow(
                device_instance_id=device_ref.device_instance_id,
                owner_domain_id=str(device_ref.owner_domain_id),
                business_owner_id="owner_01",
                hardware_identity_ref="hardware_01",
                owner_domain_generation=device_ref.owner_domain_generation,
                claim_generation=device_ref.claim_generation,
                trust_epoch=device_ref.trust_epoch,
                manifest_ref_json=json.dumps(
                    {"manifest_id": "manifest_01", "revision": 1, "digest": MANIFEST}
                ),
                approval_decision_id="decision_7",
                operational_public_key_spki=_spki(key),
                state="revoked",
                revision=8,
                activated_at=occurred_at - timedelta(minutes=1),
                updated_at=occurred_at,
                revoked_at=occurred_at,
            )
        )
        session.add(
            AdmissionClaimEventStreamRow(
                event_id=event["id"],
                owner_domain_id="owner-domain_01",
                event_type=event["type"],
                event_json=json.dumps(event, sort_keys=True, separators=(",", ":")),
                occurred_at=occurred_at,
            )
        )


@pytest.fixture
async def database(tmp_path):
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await database.initialize_schema()
    try:
        yield database
    finally:
        await database.close()


async def _materialize(database, key, *, ttl=timedelta(days=7)):
    ledger = SqlDeviceEraseLedger(database)
    await _seed_revoke(database, key)
    assert await ledger.materialize_claim_events(now=NOW, operation_ttl=ttl) == 1
    operation = await ledger.get_for_device(device_ref=_ref())
    assert operation is not None
    return ledger, operation


def _ack(key, operation_id, *, device_ref=None, result_code="ERASED"):
    values = {
        "contract": "eidolon.device-foundation.device-operation-ack",
        "contract_version": "1.0",
        "operation_id": operation_id,
        "operation_type": "device-local.erase",
        "device_ref": (device_ref or _ref()).model_dump(mode="json"),
        "ack_sequence": 1,
        "result": "erased",
        "result_code": result_code,
        "device_monotonic_time": 1234,
    }
    return DeviceLocalEraseAck(**values, device_signature=_sign(key, values))


async def test_canonical_revoke_materializes_operation_and_signed_ack(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger, operation = await _materialize(database, key)
    reconcile = ReconcileDeviceEraseOperations(
        ledger=ledger, clock=Clock(), operation_ttl=timedelta(days=7)
    )
    assert await reconcile.execute() == 0
    pending = await ledger.get(operation_id=operation.command.operation_id)
    assert pending is not None and pending.state is DeviceEraseState.PENDING

    nonce = "fresh_nonce_000001"
    document = {
        "device_ref": _ref().model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-local.erase",
    }
    delivery = await PullDeviceEraseOperation(
        ledger=ledger, clock=Clock(), operation_ttl=timedelta(days=7)
    ).execute(
        device_ref=_ref(),
        public_key_spki=_spki(key),
        nonce=nonce,
        signature=_sign(key, document),
    )
    assert delivery is not None
    terminal = await AcknowledgeDeviceEraseOperation(ledger=ledger, clock=Clock()).execute(
        ack=_ack(key, operation.command.operation_id)
    )
    assert terminal.state is DeviceEraseState.ACKNOWLEDGED


async def test_restart_preserves_event_derived_operation(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    _ledger, pending = await _materialize(database, key)
    relocated = SqlDeviceEraseLedger(database)
    assert (
        await relocated.materialize_claim_events(
            now=NOW + timedelta(hours=1), operation_ttl=timedelta(days=7)
        )
        == 0
    )
    recovered = await relocated.get_for_device(device_ref=_ref())
    assert recovered is not None and recovered.command == pending.command


async def test_late_ack_and_conflicting_duplicate_fail_closed(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger, operation = await _materialize(database, key, ttl=timedelta(minutes=5))
    clock = Clock(NOW + timedelta(minutes=6))
    assert await ledger.expire_due(now=clock.now()) == 1
    service = AcknowledgeDeviceEraseOperation(ledger=ledger, clock=clock)
    unchanged = await service.execute(ack=_ack(key, operation.command.operation_id))
    assert unchanged.state is DeviceEraseState.EXPIRED
    with pytest.raises(DeviceEraseIdempotencyConflict):
        await service.execute(
            ack=_ack(key, operation.command.operation_id, result_code="DIFFERENT")
        )


async def test_a_device_that_comes_back_late_is_still_told_to_erase(database) -> None:
    """A lapsed deadline ends one delivery attempt, not the Owner's instruction.

    The device this matters for is the one that was broken or unplugged for
    longer than the TTL. It is also the only device for which local erase is
    still possible at all — and it was the one the Host had nothing left to say
    to: the operation went terminal `expired`, the pull answered 204 forever,
    and a device carrying Owner data could rejoin the network without ever
    being told to drop it.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    ledger, operation = await _materialize(database, key, ttl=timedelta(minutes=5))
    lapsed = Clock(NOW + timedelta(days=30))
    assert await ledger.expire_due(now=lapsed.now()) == 1

    nonce = "returning_nonce_01"
    document = {
        "device_ref": _ref().model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-local.erase",
    }
    delivery = await PullDeviceEraseOperation(
        ledger=ledger, clock=lapsed, operation_ttl=timedelta(minutes=5)
    ).execute(
        device_ref=_ref(),
        public_key_spki=_spki(key),
        nonce=nonce,
        signature=_sign(key, document),
    )
    assert delivery is not None
    # The same instruction, re-armed: one Owner decision, not a second one.
    assert delivery.command.operation_id == operation.command.operation_id
    assert delivery.command.deadline > lapsed.now()

    terminal = await AcknowledgeDeviceEraseOperation(ledger=ledger, clock=lapsed).execute(
        ack=_ack(key, operation.command.operation_id)
    )
    assert terminal.state is DeviceEraseState.ACKNOWLEDGED
    assert terminal.terminal_result == "erased"


async def test_an_acknowledged_erase_is_never_re_armed(database) -> None:
    """Re-arming is for an unfinished instruction, never a finished one."""

    key = ec.generate_private_key(ec.SECP256R1())
    ledger, operation = await _materialize(database, key, ttl=timedelta(minutes=5))
    await AcknowledgeDeviceEraseOperation(ledger=ledger, clock=Clock()).execute(
        ack=_ack(key, operation.command.operation_id)
    )
    lapsed = Clock(NOW + timedelta(days=30))

    nonce = "acknowledged_nonce1"
    document = {
        "device_ref": _ref().model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-local.erase",
    }
    assert (
        await PullDeviceEraseOperation(
            ledger=ledger, clock=lapsed, operation_ttl=timedelta(minutes=5)
        ).execute(
            device_ref=_ref(),
            public_key_spki=_spki(key),
            nonce=nonce,
            signature=_sign(key, document),
        )
        is None
    )
    settled = await ledger.get(operation_id=operation.command.operation_id)
    assert settled is not None and settled.state is DeviceEraseState.ACKNOWLEDGED


async def test_re_arming_needs_the_device_proof_first(database) -> None:
    """An unauthenticated caller cannot move the ledger by asking.

    The re-arm is a write, and it happens on the pull path — so the key and the
    signature have to be checked before it, or a stranger who knows a DeviceRef
    could keep an expired instruction alive.
    """

    key = ec.generate_private_key(ec.SECP256R1())
    stranger = ec.generate_private_key(ec.SECP256R1())
    ledger, operation = await _materialize(database, key, ttl=timedelta(minutes=5))
    lapsed = Clock(NOW + timedelta(days=30))
    assert await ledger.expire_due(now=lapsed.now()) == 1

    nonce = "stranger_nonce_0001"
    document = {
        "device_ref": _ref().model_dump(mode="json"),
        "nonce": nonce,
        "operation_type": "device-local.erase",
    }
    with pytest.raises(PermissionError):
        await PullDeviceEraseOperation(
            ledger=ledger, clock=lapsed, operation_ttl=timedelta(minutes=5)
        ).execute(
            device_ref=_ref(),
            public_key_spki=_spki(stranger),
            nonce=nonce,
            signature=_sign(stranger, document),
        )
    untouched = await ledger.get(operation_id=operation.command.operation_id)
    assert untouched is not None and untouched.state is DeviceEraseState.EXPIRED


async def test_old_generation_ack_cannot_change_operation(database) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ledger, operation = await _materialize(database, key)
    with pytest.raises(DeviceEraseGenerationConflict):
        await AcknowledgeDeviceEraseOperation(ledger=ledger, clock=Clock()).execute(
            ack=_ack(key, operation.command.operation_id, device_ref=_ref(6))
        )
