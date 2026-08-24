from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import rfc8785
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eidolon_sdk.device_foundation.v1 import (
    AckClaimGrantResult,
    BusinessOwnerId,
    CancelEnrollmentResult,
    ClaimEventCursor,
    ClaimEventPage,
    ClaimQuery,
    ClaimState,
    CollectClaimGrantResult,
    ControllerActorRef,
    CreateEnrollmentResult,
    DecideEnrollmentResult,
    DeviceProblem,
    DeviceRef,
    EnrollmentProposalQuery,
    EnrollmentProposalState,
    OwnerDomainId,
    RevokeClaimResult,
)
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    AdmissionClaimEventStreamRow,
    AdmissionClaimRow,
    AdmissionCommandResultRow,
    AdmissionDecisionRow,
    AdmissionGrantAckRow,
    AdmissionGrantRow,
    AdmissionOutboxRow,
    AdmissionProposalRow,
    DeviceRow,
)
from hub.admission import crypto as admission_crypto
from hub.admission.application import AdmissionAuthority, HmacCommissioningProofVerifier
from hub.admission.crypto import key_id
from hub.admission.domain import ActorContext, AdmissionProblem
from hub.admission.http import create_admission_router
from hub.admission.persistence import SqlAdmissionStore
from hub.admission.target_app import create_admission_target_app


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)

    def now(self):
        return self.value


class SequenceIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, prefix: str) -> str:
        self.counts[prefix] = self.counts.get(prefix, 0) + 1
        return f"{prefix}_{self.counts[prefix]:02d}"


def spki(key: ec.EllipticCurvePrivateKey) -> str:
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "p256-spki:" + base64.urlsafe_b64encode(der).rstrip(b"=").decode()


def sign(key: ec.EllipticCurvePrivateKey, document: dict) -> str:
    der = key.sign(rfc8785.dumps(document), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return (
        base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        .rstrip(b"=")
        .decode()
    )


def open_wire_envelope(recipient: ec.EllipticCurvePrivateKey, envelope: dict, aad: dict) -> dict:
    enc = admission_crypto._decode(envelope["encapsulated_key"])  # noqa: SLF001
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), enc)
    recipient_point = recipient.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    kem_suite = b"KEM" + (16).to_bytes(2, "big")
    shared = admission_crypto._labeled_expand(  # noqa: SLF001
        kem_suite,
        admission_crypto._labeled_extract(  # noqa: SLF001
            kem_suite,
            b"",
            b"eae_prk",
            recipient.exchange(ec.ECDH(), ephemeral),
        ),
        b"shared_secret",
        enc + recipient_point,
        32,
    )
    suite = b"HPKE" + (16).to_bytes(2, "big") + (1).to_bytes(2, "big") * 2
    context = (
        b"\x00"
        + admission_crypto._labeled_extract(  # noqa: SLF001
            suite, b"", b"psk_id_hash", b""
        )
        + admission_crypto._labeled_extract(  # noqa: SLF001
            suite, b"", b"info_hash", b"eidolon-trust-p256-hpke-v1"
        )
    )
    secret = admission_crypto._labeled_extract(  # noqa: SLF001
        suite, shared, b"secret", b""
    )
    key = admission_crypto._labeled_expand(  # noqa: SLF001
        suite, secret, b"key", context, 16
    )
    nonce = admission_crypto._labeled_expand(  # noqa: SLF001
        suite, secret, b"base_nonce", context, 12
    )
    plaintext = AESGCM(key).decrypt(
        nonce,
        admission_crypto._decode(envelope["ciphertext"]),  # noqa: SLF001
        rfc8785.dumps(aad),
    )
    return json.loads(plaintext)


@pytest.fixture
async def harness(tmp_path):
    database = HubDatabase.sqlite(
        tmp_path / "hub.sqlite3",
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
    )
    await database.initialize_schema()
    clock = FixedClock()
    ids = SequenceIds()
    setup_secret = b"per-device-setup-secret"
    authority = AdmissionAuthority(
        store=SqlAdmissionStore(database),
        clock=clock,
        ids=ids,
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=HmacCommissioningProofVerifier(
            lambda device_id: setup_secret if device_id == "device_01" else None
        ),
    )
    yield database, authority, clock, setup_secret
    await database.close()


def actor(
    *,
    domain: str = "owner-domain_01",
    owner: str = "owner_01",
    principal: str = "controller_01",
) -> ActorContext:
    domain_id = OwnerDomainId(domain)
    return ActorContext(
        actor=ControllerActorRef(
            principal_id=principal,
            owner_domain_id=domain_id,
            granted_scopes=(
                "device.read",
                "device.claim.approve",
                "device.claim.revoke",
                "device.claim.events.read",
            ),
            authentication_strength="hardware-backed",
        ),
        owner_domain_id=domain_id,
        business_owner_id=BusinessOwnerId(owner),
    )


def create_payload(handoff_key, operational_key, setup_secret) -> dict:
    evidence = "manufacturer-evidence-device-01"
    manifest_document = {
        "schema_version": 1,
        "title": "Box-3",
        "properties": [],
        "actions": [],
        "events": [],
        "media": [],
    }
    nonce = "commissioning-nonce-01"
    message = f"device_01\0owner-domain_01\0{nonce}".encode()
    proof = (
        base64.urlsafe_b64encode(hmac.new(setup_secret, message, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    return {
        "profile_id": "eidolon-trust-p256-hpke-v1",
        "device_instance_candidate_id": "device_01",
        "requested_owner_domain_id": "owner-domain_01",
        "hardware_identity_evidence": {
            "scheme": "manufacturer-p256",
            "evidence": evidence,
            "evidence_digest": "sha256:" + hashlib.sha256(evidence.encode()).hexdigest(),
        },
        "commissioning_proof": {
            "scheme": "protocomm-security2-srp6a-aes256gcm",
            "proof": proof,
            "nonce": nonce,
        },
        "manifest": {
            "manifest_id": "manifest_01",
            "revision": 2,
            "digest": "sha256:" + hashlib.sha256(rfc8785.dumps(manifest_document)).hexdigest(),
            "document": manifest_document,
        },
        "handoff_key": {
            "scheme": "DHKEM-P256-HKDF-SHA256",
            "public_key": spki(handoff_key),
        },
        "operational_key": {
            "scheme": "ES256-P256",
            "public_key": spki(operational_key),
        },
    }


async def create_and_approve(harness):
    _database, authority, _clock, secret = harness
    handoff_key = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational_key = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(handoff_key, operational_key, secret),
    )
    decision = await authority.decide_enrollment(
        command_id="decide_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        payload={
            "expected_proposal_revision": 1,
            "decision": "approve",
            "target_owner_domain_id": "owner-domain_01",
            "target_business_owner_id": "owner_01",
            "target_space_id": None,
            "reviewed_manifest_ref": created["reviewed_manifest_ref"],
            "initial_assignment_intent": None,
            "initial_capability_policy_refs": [],
        },
        context=actor(),
    )
    return created, decision, handoff_key, operational_key


async def collect_and_ack(harness):
    database, authority, _clock, _secret = harness
    created, decision, handoff, operational = await create_and_approve(harness)
    collection_document = {
        "contract": "eidolon.device-foundation.claim-grant-collection",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "collection_challenge": created["collection_challenge"],
    }
    collected = await authority.collect_claim_grant(
        command_id="collect_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
        handoff_key_proof=sign(handoff, collection_document),
    )
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, collected["grant_id"])
        device_ref = DeviceRef.model_validate_json(grant.device_ref_json)
    ack_document = {
        "contract": "eidolon.device-foundation.claim-grant-ack",
        "enrollment_id": created["enrollment_id"],
        "grant_id": collected["grant_id"],
        "device_ref": device_ref.model_dump(mode="json"),
    }
    ack_proof = sign(operational, ack_document)
    active = await authority.ack_claim_grant(
        command_id="ack_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        operational_key_proof=ack_proof,
        stored_claim_generation=device_ref.claim_generation,
        stored_trust_epoch=device_ref.trust_epoch,
    )
    return created, decision, collected, device_ref, ack_proof, active


async def test_characterization_network_or_provider_success_cannot_imply_decision_or_claim(harness):
    database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(handoff, operational, secret),
    )
    async with database.sessions() as session:
        proposal = await session.get(AdmissionProposalRow, created["enrollment_id"])
        assert proposal.state == "pending_review"
        assert await session.scalar(select(func.count()).select_from(AdmissionDecisionRow)) == 0
        assert await session.scalar(select(func.count()).select_from(AdmissionClaimRow)) == 0


async def test_decision_collection_ack_activate_once_and_restart_replays_first_result(harness):
    database, authority, _clock, _secret = harness
    created, decision, handoff, operational = await create_and_approve(harness)
    assert decision["decision"]["target_owner_domain_id"] == "owner-domain_01"
    assert decision["decision"]["target_business_owner_id"] == "owner_01"
    proof_doc = {
        "contract": "eidolon.device-foundation.claim-grant-collection",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "collection_challenge": created["collection_challenge"],
    }
    collection_proof = sign(handoff, proof_doc)
    collected = await authority.collect_claim_grant(
        command_id="collect_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
        handoff_key_proof=collection_proof,
    )
    redelivered = await authority.collect_claim_grant(
        command_id="collect_02",
        correlation_id="intent_reply_lost",
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
        handoff_key_proof=collection_proof,
    )
    assert redelivered["wire_envelope"] == collected["wire_envelope"]
    assert redelivered["occurred_at"] == collected["occurred_at"]
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, collected["grant_id"])
        device_ref = DeviceRef.model_validate_json(grant.device_ref_json)
    ack_doc = {
        "contract": "eidolon.device-foundation.claim-grant-ack",
        "enrollment_id": created["enrollment_id"],
        "grant_id": collected["grant_id"],
        "device_ref": device_ref.model_dump(mode="json"),
    }
    ack_proof = sign(operational, ack_doc)
    active = await authority.ack_claim_grant(
        command_id="ack_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        operational_key_proof=ack_proof,
        stored_claim_generation=device_ref.claim_generation,
        stored_trust_epoch=device_ref.trust_epoch,
    )
    first_time = active["occurred_at"]
    database_path = database.engine.url.database
    await database.close()
    restarted_database = HubDatabase.sqlite(
        database_path,
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
    )
    await restarted_database.initialize_schema()
    restarted = AdmissionAuthority(
        store=SqlAdmissionStore(restarted_database),
        clock=authority.clock,
        ids=SequenceIds(),
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=authority.commissioning_proofs,
    )
    try:
        replay = await restarted.ack_claim_grant(
            command_id="ack_01",
            correlation_id="different-audit",
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            operational_key_proof=ack_proof,
            stored_claim_generation=device_ref.claim_generation,
            stored_trust_epoch=device_ref.trust_epoch,
        )
        assert replay["outcome"] == "replayed"
        assert replay["occurred_at"] == first_time
        with pytest.raises(AdmissionProblem) as terminal_handoff:
            await restarted.collect_claim_grant(
                command_id="collect_after_claim_active",
                correlation_id="intent_after_claim_active",
                enrollment_id=created["enrollment_id"],
                proposal_revision=1,
                collection_challenge=created["collection_challenge"],
                handoff_key_proof=collection_proof,
            )
        assert terminal_handoff.value.code == "PROPOSAL_TERMINAL"
        async with restarted_database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AdmissionGrantAckRow)) == 1
            assert await session.scalar(select(func.count()).select_from(AdmissionClaimRow)) == 1
            directory = await session.get(DeviceRow, device_ref.device_instance_id)
            assert directory.owner_id == "owner_01"
            assert directory.lifecycle_state == "approved"
            assert directory.claim_generation == device_ref.claim_generation
            assert directory.display_name == "Box-3"
            activated = await session.scalar(
                select(func.count())
                .select_from(AdmissionOutboxRow)
                .where(AdmissionOutboxRow.event_type == "live.eidolon.device.claim-activated.v1")
            )
            assert activated == 1
            delivered = await session.scalar(
                select(func.count())
                .select_from(AdmissionOutboxRow)
                .where(
                    AdmissionOutboxRow.event_type == "live.eidolon.device.claim-grant-delivered.v1"
                )
            )
            assert delivered == 1
            ordered_types = tuple(
                await session.scalars(
                    select(AdmissionOutboxRow.event_type).order_by(AdmissionOutboxRow.event_id)
                )
            )
            assert ordered_types.index(
                "live.eidolon.device.claim-grant-acknowledged.v1"
            ) < ordered_types.index("live.eidolon.device.claim-activated.v1")
    finally:
        await restarted_database.close()


async def test_same_command_different_payload_conflicts_and_invalid_proof_rolls_back(harness):
    database, authority, _clock, _secret = harness
    created, _decision, _handoff, _operational = await create_and_approve(harness)
    with pytest.raises(AdmissionProblem) as invalid:
        await authority.collect_claim_grant(
            command_id="collect_01",
            correlation_id="intent_01",
            enrollment_id=created["enrollment_id"],
            proposal_revision=1,
            collection_challenge=created["collection_challenge"],
            handoff_key_proof="invalid",
        )
    assert invalid.value.code == "HANDOFF_PROOF_INVALID"
    async with database.sessions() as session:
        proposal = await session.get(AdmissionProposalRow, created["enrollment_id"])
        assert proposal.state == "approved_awaiting_handoff"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdmissionCommandResultRow)
                .where(AdmissionCommandResultRow.command_id == "collect_01")
            )
            == 0
        )
    payload = create_payload(
        ec.generate_private_key(ec.SECP256R1()),
        ec.generate_private_key(ec.SECP256R1()),
        b"per-device-setup-secret",
    )
    with pytest.raises(AdmissionProblem) as conflict:
        await authority.create_enrollment(
            command_id="create_01", correlation_id="intent_02", payload=payload
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


async def test_pre_b0_opaque_grant_is_not_domain_replayed_or_double_emitted(harness):
    database, authority, clock, _secret = harness
    created, _decision, handoff, _operational = await create_and_approve(harness)
    proof_doc = {
        "contract": "eidolon.device-foundation.claim-grant-collection",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "collection_challenge": created["collection_challenge"],
    }
    async with database.sessions.begin() as session:
        proposal = await session.get(AdmissionProposalRow, created["enrollment_id"])
        grant = await session.get(AdmissionGrantRow, _decision["grant_id"])
        proposal.state = "grant_delivered"
        proposal.revision = 3
        proposal.updated_at = clock.now()
        grant.legacy_sealed_grant = "pre-b0-opaque-ciphertext"
        grant.delivered_at = clock.now()
    with pytest.raises(AdmissionProblem) as unsupported:
        await authority.collect_claim_grant(
            command_id="collect_pre_b0",
            correlation_id="intent_pre_b0",
            enrollment_id=created["enrollment_id"],
            proposal_revision=1,
            collection_challenge=created["collection_challenge"],
            handoff_key_proof=sign(handoff, proof_doc),
        )
    assert unsupported.value.code == "CONTRACT_UNSUPPORTED"
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, _decision["grant_id"])
        assert grant.wire_envelope_json is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdmissionOutboxRow)
                .where(
                    AdmissionOutboxRow.event_type == "live.eidolon.device.claim-grant-delivered.v1"
                )
            )
            == 0
        )


async def test_manifest_precondition_and_controller_scope_fail_closed(harness):
    _database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    payload = create_payload(handoff, operational, secret)
    payload["manifest"]["document"] = {"endpoints": ["mutated"]}
    with pytest.raises(AdmissionProblem) as invalid_manifest:
        await authority.create_enrollment(
            command_id="create_bad_manifest",
            correlation_id="intent_bad_manifest",
            payload=payload,
        )
    assert invalid_manifest.value.code == "INVALID_ARGUMENT"

    valid_payload = create_payload(handoff, operational, secret)
    created = await authority.create_enrollment(
        command_id="create_01", correlation_id="intent_01", payload=valid_payload
    )
    domain_id = OwnerDomainId("owner-domain_01")
    unprivileged = ActorContext(
        actor=ControllerActorRef(
            principal_id="controller_read_only",
            owner_domain_id=domain_id,
            granted_scopes=("device.read",),
            authentication_strength="hardware-backed",
        ),
        owner_domain_id=domain_id,
        business_owner_id=BusinessOwnerId("owner_01"),
    )
    with pytest.raises(AdmissionProblem) as forbidden:
        await authority.decide_enrollment(
            command_id="decide_without_scope",
            correlation_id="intent_01",
            enrollment_id=created["enrollment_id"],
            payload={
                "expected_proposal_revision": 1,
                "decision": "approve",
                "target_owner_domain_id": "owner-domain_01",
                "target_business_owner_id": "owner_01",
                "reviewed_manifest_ref": created["reviewed_manifest_ref"],
            },
            context=unprivileged,
        )
    assert forbidden.value.code == "FORBIDDEN"


async def test_revoke_approved_awaiting_handoff_fences_old_grant_and_ack(harness):
    database, authority, _clock, _secret = harness
    created, decision, handoff, _operational = await create_and_approve(harness)
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, decision["grant_id"])
        device_ref = DeviceRef.model_validate_json(grant.device_ref_json)
    revoked = await authority.revoke_claim(
        command_id="revoke_01",
        correlation_id="intent_02",
        device_ref=device_ref,
        reason="owner-removed",
        context=actor(),
    )
    assert revoked["claim_state"] == "revoked"
    proof_doc = {
        "contract": "eidolon.device-foundation.claim-grant-collection",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "collection_challenge": created["collection_challenge"],
    }
    with pytest.raises(AdmissionProblem) as stale:
        await authority.collect_claim_grant(
            command_id="collect_after_revoke",
            correlation_id="intent_02",
            enrollment_id=created["enrollment_id"],
            proposal_revision=1,
            collection_challenge=created["collection_challenge"],
            handoff_key_proof=sign(handoff, proof_doc),
        )
    assert stale.value.code == "CLAIM_REVOKED"


async def test_owner_scoped_query_does_not_leak_cross_owner_and_outbox_failure_recovers(harness):
    database, authority, clock, _secret = harness
    created, decision, _handoff, _operational = await create_and_approve(harness)
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, decision["grant_id"])
        ref = DeviceRef.model_validate_json(grant.device_ref_json)
    await authority.revoke_claim(
        command_id="revoke_01",
        correlation_id="intent_02",
        device_ref=ref,
        reason="owner-removed",
        context=actor(),
    )
    with pytest.raises(AdmissionProblem) as hidden:
        await authority.get_claim(
            device_instance_id=ref.device_instance_id, context=actor(owner="owner_02")
        )
    assert hidden.value.code == "NOT_FOUND"
    pending = await authority.store.pending_outbox()
    event_id = pending[0]["id"]
    await authority.store.record_publish_failure(event_id=event_id, error="broker unavailable")
    assert any(event["id"] == event_id for event in await authority.store.pending_outbox())
    await authority.store.mark_published(event_id=event_id, published_at=clock.now())
    assert all(event["id"] != event_id for event in await authority.store.pending_outbox())


async def test_reply_loss_and_concurrent_same_command_replay_one_durable_result(harness):
    database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    payload = create_payload(handoff, operational, secret)

    first, concurrent_replay = await asyncio.gather(
        authority.create_enrollment(
            command_id="create_01", correlation_id="intent_first", payload=payload
        ),
        authority.create_enrollment(
            command_id="create_01", correlation_id="intent_reply_lost", payload=payload
        ),
    )
    assert {first["outcome"], concurrent_replay["outcome"]} == {"committed", "replayed"}
    durable = first if first["outcome"] == "committed" else concurrent_replay
    replay = await authority.create_enrollment(
        command_id="create_01", correlation_id="intent_after_restart", payload=payload
    )
    assert replay["enrollment_id"] == durable["enrollment_id"]
    assert replay["occurred_at"] == durable["occurred_at"]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdmissionProposalRow)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(AdmissionCommandResultRow)) == 1
        )
        assert await session.scalar(select(func.count()).select_from(AdmissionOutboxRow)) == 1


async def test_transaction_rolls_back_proposal_result_and_outbox_on_event_conflict(harness):
    database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    payload = create_payload(handoff, operational, secret)
    await authority.create_enrollment(
        command_id="create_01", correlation_id="intent_01", payload=payload
    )

    class CollisionIds:
        def new(self, prefix: str) -> str:
            return "enrollment_rollback" if prefix == "enrollment" else "admission-event_01"

    colliding = AdmissionAuthority(
        store=SqlAdmissionStore(database),
        clock=authority.clock,
        ids=CollisionIds(),
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=authority.commissioning_proofs,
    )
    with pytest.raises(IntegrityError):
        await colliding.create_enrollment(
            command_id="create_rollback", correlation_id="intent_rollback", payload=payload
        )
    async with database.sessions() as session:
        assert await session.get(AdmissionProposalRow, "enrollment_rollback") is None
        assert (
            await session.get(
                AdmissionCommandResultRow,
                ("owner-domain_01", "admission.create-enrollment", "create_rollback"),
            )
            is None
        )
        assert await session.scalar(select(func.count()).select_from(AdmissionOutboxRow)) == 1


async def test_reject_cancel_and_deadline_expiry_are_distinct_terminal_facts(harness):
    database, authority, clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    payload = create_payload(handoff, operational, secret)

    rejected = await authority.create_enrollment(
        command_id="create_reject", correlation_id="intent_reject", payload=payload
    )
    await authority.decide_enrollment(
        command_id="decide_reject",
        correlation_id="intent_reject",
        enrollment_id=rejected["enrollment_id"],
        payload={
            "expected_proposal_revision": 1,
            "decision": "reject",
            "target_owner_domain_id": "owner-domain_01",
            "target_business_owner_id": "owner_01",
            "target_space_id": None,
            "reviewed_manifest_ref": rejected["reviewed_manifest_ref"],
            "initial_assignment_intent": None,
            "initial_capability_policy_refs": [],
        },
        context=actor(),
    )
    canceled = await authority.create_enrollment(
        command_id="create_cancel", correlation_id="intent_cancel", payload=payload
    )
    cancel_result = await authority.cancel_enrollment(
        command_id="cancel_01",
        correlation_id="intent_cancel",
        enrollment_id=canceled["enrollment_id"],
        reason="controller-canceled",
        context=actor(),
    )
    assert cancel_result["proposal_state"] == "canceled"
    expiring = await authority.create_enrollment(
        command_id="create_expire", correlation_id="intent_expire", payload=payload
    )
    clock.value += timedelta(minutes=16)
    assert await authority.expire_due() == 1
    assert await authority.expire_due() == 0
    with pytest.raises(AdmissionProblem) as expired_decision:
        await authority.decide_enrollment(
            command_id="decide_expired",
            correlation_id="intent_expire",
            enrollment_id=expiring["enrollment_id"],
            payload={
                "expected_proposal_revision": 2,
                "decision": "approve",
                "target_owner_domain_id": "owner-domain_01",
                "target_business_owner_id": "owner_01",
                "reviewed_manifest_ref": expiring["reviewed_manifest_ref"],
            },
            context=actor(),
        )
    assert expired_decision.value.code == "PROPOSAL_EXPIRED"
    async with database.sessions() as session:
        states = {
            row.enrollment_id: row.state
            for row in (await session.scalars(select(AdmissionProposalRow))).all()
        }
        assert states == {
            rejected["enrollment_id"]: "rejected",
            canceled["enrollment_id"]: "canceled",
            expiring["enrollment_id"]: "expired",
        }
        assert await session.scalar(select(func.count()).select_from(AdmissionGrantRow)) == 0


async def test_concurrent_controllers_commit_one_immutable_decision(harness):
    database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(handoff, operational, secret),
    )
    payload = {
        "expected_proposal_revision": 1,
        "decision": "approve",
        "target_owner_domain_id": "owner-domain_01",
        "target_business_owner_id": "owner_01",
        "reviewed_manifest_ref": created["reviewed_manifest_ref"],
    }
    outcomes = await asyncio.gather(
        authority.decide_enrollment(
            command_id="decide_controller_a",
            correlation_id="intent_01",
            enrollment_id=created["enrollment_id"],
            payload=payload,
            context=actor(principal="controller_a"),
        ),
        authority.decide_enrollment(
            command_id="decide_controller_b",
            correlation_id="intent_01",
            enrollment_id=created["enrollment_id"],
            payload=payload,
            context=actor(principal="controller_b"),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    problem = next(value for value in outcomes if isinstance(value, AdmissionProblem))
    assert problem.code == "REVISION_CONFLICT"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdmissionDecisionRow)) == 1
        assert await session.scalar(select(func.count()).select_from(AdmissionGrantRow)) == 1


async def test_active_revoke_fences_old_grant_ack_and_stale_generation(harness):
    database, authority, _clock, _secret = harness
    created, _decision, collected, ref, ack_proof, _active = await collect_and_ack(harness)
    revoked = await authority.revoke_claim(
        command_id="revoke_active",
        correlation_id="intent_revoke",
        device_ref=ref,
        reason="owner-removed",
        context=actor(),
    )
    assert revoked["claim_state"] == "revoked"
    with pytest.raises(AdmissionProblem) as old_ack:
        await authority.ack_claim_grant(
            command_id="ack_after_revoke",
            correlation_id="intent_revoke",
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            operational_key_proof=ack_proof,
            stored_claim_generation=ref.claim_generation,
            stored_trust_epoch=ref.trust_epoch,
        )
    assert old_ack.value.code == "CLAIM_REVOKED"
    with pytest.raises(AdmissionProblem) as stale:
        await authority.revoke_claim(
            command_id="revoke_stale",
            correlation_id="intent_revoke",
            device_ref=ref.model_copy(update={"claim_generation": ref.claim_generation + 1}),
            reason="owner-removed",
            context=actor(),
        )
    assert stale.value.code == "GENERATION_CONFLICT"
    second_terminal = await authority.revoke_claim(
        command_id="revoke_terminal_again",
        correlation_id="intent_revoke",
        device_ref=ref,
        reason="owner-removed",
        context=actor(),
    )
    assert second_terminal["occurred_at"] == revoked["occurred_at"]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdmissionGrantAckRow)) == 1
        revoked_events = await session.scalar(
            select(func.count())
            .select_from(AdmissionOutboxRow)
            .where(AdmissionOutboxRow.event_type == "live.eidolon.device.claim-revoked.v1")
        )
        assert revoked_events == 1


async def test_http_problem_mapping_preserves_owner_mismatch_and_actor_is_not_body_supplied(
    harness,
):
    _database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(handoff, operational, secret),
    )

    async def actor_provider(_request):
        return actor()

    app = FastAPI()
    app.include_router(create_admission_router(authority=authority, actor_provider=actor_provider))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/admission/v1/enrollments/{created['enrollment_id']}/decisions",
            json={
                "command_id": "decide_bad_owner",
                "correlation_id": "intent_01",
                "expected_proposal_revision": 1,
                "decision": "approve",
                "target_owner_domain_id": "owner-domain_01",
                "target_business_owner_id": "owner_02",
                "reviewed_manifest_ref": created["reviewed_manifest_ref"],
                "actor": {"principal_id": "attacker_body_actor"},
            },
        )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "INVALID_ARGUMENT"
    assert response.json()["retryable"] is False
    assert "unknown=['actor']" in response.json()["detail"]
    assert set(response.json()) == set(DeviceProblem.model_fields)
    DeviceProblem.model_validate(response.json())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/admission/v1/enrollments/{created['enrollment_id']}/decisions",
            json={
                "command_id": "decide_bad_owner",
                "correlation_id": "intent_01",
                "enrollment_id": created["enrollment_id"],
                "expected_proposal_revision": 1,
                "decision": "approve",
                "target_owner_domain_id": "owner-domain_01",
                "target_business_owner_id": "owner_02",
                "target_space_id": None,
                "reviewed_manifest_ref": created["reviewed_manifest_ref"],
                "initial_assignment_intent": None,
                "initial_capability_policy_refs": [],
            },
        )
    assert response.status_code == 403
    assert response.json()["code"] == "BUSINESS_OWNER_MISMATCH"


async def test_claim_wire_envelope_is_preopen_complete_and_replay_stable(harness):
    _database, authority, _clock, _secret = harness
    created, _decision, handoff, _operational = await create_and_approve(harness)
    proof_doc = {
        "contract": "eidolon.device-foundation.claim-grant-collection",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "collection_challenge": created["collection_challenge"],
    }
    collection_proof = sign(handoff, proof_doc)
    collected = await authority.collect_claim_grant(
        command_id="collect_wire_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
        handoff_key_proof=collection_proof,
    )
    replay = await authority.collect_claim_grant(
        command_id="collect_wire_01",
        correlation_id="different_audit_metadata",
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
        handoff_key_proof=collection_proof,
    )
    assert "sealed_grant" not in collected
    assert replay["wire_envelope"] == collected["wire_envelope"]
    assert replay["occurred_at"] == collected["occurred_at"]
    envelope = collected["wire_envelope"]
    assert envelope["profile_id"] == "eidolon-trust-p256-hpke-v1"
    assert envelope["kem"] == "DHKEM-P256-HKDF-SHA256"
    assert envelope["kdf"] == "HKDF-SHA256"
    assert envelope["aead"] == "AES-128-GCM"
    assert envelope["recipient_handoff_key_id"] == key_id(spki(handoff))
    assert envelope["aad"] == {
        "contract": "eidolon.device-foundation.claim-grant-aad",
        "profile_id": "eidolon-trust-p256-hpke-v1",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "device_instance_id": "device_01",
        "hardware_evidence_digest": envelope["aad"]["hardware_evidence_digest"],
        "manifest_ref": created["reviewed_manifest_ref"],
        "owner_domain_id": "owner-domain_01",
        "owner_domain_generation": 3,
        "claim_generation": 1,
        "trust_epoch": 1,
        "grant_id": collected["grant_id"],
    }
    plaintext = open_wire_envelope(handoff, envelope, envelope["aad"])
    assert plaintext["grant_id"] == collected["grant_id"]
    assert plaintext["device_ref"]["owner_domain_id"] == "owner-domain_01"
    tampered_aad = {**envelope["aad"], "device_instance_id": "device_other"}
    with pytest.raises(InvalidTag):
        open_wire_envelope(handoff, envelope, tampered_aad)


async def test_claim_stream_dataschema_cursor_replay_publish_failure_and_restart(harness):
    database, authority, _clock, _secret = harness
    _created, _decision, _collected, device_ref, _proof, _active = await collect_and_ack(harness)
    first = await authority.claim_event_page(
        cursor=ClaimEventCursor(stream_position=0), limit=100, context=actor()
    )
    assert isinstance(first, ClaimEventPage)
    assert [item.stream_position for item in first.events] == [1]
    activated = first.events[0].event
    assert activated.dataschema.endswith("claim-activated-data.schema.json")
    assert activated.audience == "eidolon-claim-consumers"
    await authority.store.record_publish_failure(event_id=activated.id, error="broker down")
    pending_after_failure = await authority.store.pending_outbox()
    assert activated.id in {event["id"] for event in pending_after_failure}
    restarted_store = SqlAdmissionStore(database)
    assert activated.id in {event["id"] for event in await restarted_store.pending_outbox()}
    await restarted_store.mark_published(event_id=activated.id, published_at=datetime.now(UTC))
    assert activated.id not in {event["id"] for event in await restarted_store.pending_outbox()}

    await authority.revoke_claim(
        command_id="revoke_stream_01",
        correlation_id="intent_remove",
        device_ref=device_ref,
        reason="owner-removed",
        context=actor(),
    )
    replay = await authority.claim_event_page(
        cursor=ClaimEventCursor(stream_position=0), limit=100, context=actor()
    )
    assert [item.stream_position for item in replay.events] == [1, 2]
    assert replay.events[0].event.model_dump(mode="json") == activated.model_dump(mode="json")
    assert replay.events[1].event.dataschema.endswith("claim-revoked-data.schema.json")

    restarted = AdmissionAuthority(
        store=SqlAdmissionStore(database),
        clock=authority.clock,
        ids=SequenceIds(),
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=authority.commissioning_proofs,
    )
    resumed = await restarted.claim_event_page(
        cursor=ClaimEventCursor(stream_position=1), limit=100, context=actor()
    )
    assert [item.stream_position for item in resumed.events] == [2]
    with pytest.raises(AdmissionProblem, match="ahead") as gap:
        await restarted.claim_event_page(
            cursor=ClaimEventCursor(stream_position=99), limit=100, context=actor()
        )
    assert gap.value.code == "CURSOR_GAP"
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(AdmissionClaimEventStreamRow))
            == 2
        )


async def test_owner_scoped_proposal_claim_recovery_and_target_composition(harness):
    _database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_query_01",
        correlation_id="intent_query",
        payload=create_payload(handoff, operational, secret),
    )
    pending = await authority.get_enrollment_recovery(
        enrollment_id=created["enrollment_id"], context=actor()
    )
    assert pending.proposal.state.value == "pending_review"
    page = await authority.list_enrollment_recovery(
        query=EnrollmentProposalQuery(
            owner_domain_id="owner-domain_01",
            states=(EnrollmentProposalState.PENDING_REVIEW,),
            cursor=None,
            limit=50,
        ),
        context=actor(),
    )
    assert [item.proposal.enrollment_id for item in page.items] == [created["enrollment_id"]]

    async def actor_provider(_request):
        return actor()

    app = create_admission_target_app(authority=authority, actor_provider=actor_provider)
    paths = set(app.openapi()["paths"])
    assert "/api/admission/v1/enrollments" in paths
    assert "/api/admission/v1/claim-events" in paths
    assert not any("device-onboarding" in path or "device-management" in path for path in paths)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/admission/v1/enrollments/{created['enrollment_id']}")
        assert response.status_code == 200
        assert response.json()["proposal"]["state"] == "pending_review"
        events = await client.get("/api/admission/v1/claim-events?after_stream_position=0")
        assert events.status_code == 200
        assert events.json()["stream_id"] == "admission-claims-v1"

    claims = await authority.list_claims(
        query=ClaimQuery(
            owner_domain_id="owner-domain_other",
            states=(ClaimState.ACTIVE,),
            cursor=None,
            limit=50,
        ),
        context=actor(),
    )
    assert claims.items == ()


async def test_canonical_http_mutations_return_generated_closed_results(harness):
    _database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())

    async def actor_provider(_request):
        return actor()

    app = create_admission_target_app(authority=authority, actor_provider=actor_provider)
    transport = httpx.ASGITransport(app=app)
    payload = create_payload(handoff, operational, secret)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created_response = await client.post(
            "/api/admission/v1/enrollments",
            json={"command_id": "http_create_01", "correlation_id": "http_intent_01", **payload},
        )
        assert created_response.status_code == 201
        assert set(created_response.json()) == set(CreateEnrollmentResult.model_fields)
        created = CreateEnrollmentResult.model_validate(created_response.json())
        manifest_ref = {
            "manifest_id": payload["manifest"]["manifest_id"],
            "revision": payload["manifest"]["revision"],
            "digest": payload["manifest"]["digest"],
        }

        decision_response = await client.post(
            f"/api/admission/v1/enrollments/{created.enrollment_id}/decisions",
            json={
                "command_id": "http_decide_01",
                "correlation_id": "http_intent_01",
                "enrollment_id": created.enrollment_id,
                "expected_proposal_revision": 1,
                "decision": "approve",
                "target_owner_domain_id": "owner-domain_01",
                "target_business_owner_id": "owner_01",
                "target_space_id": None,
                "reviewed_manifest_ref": manifest_ref,
                "initial_assignment_intent": None,
                "initial_capability_policy_refs": [],
            },
        )
        assert decision_response.status_code == 200
        assert set(decision_response.json()) == set(DecideEnrollmentResult.model_fields)
        DecideEnrollmentResult.model_validate(decision_response.json())

        collection_doc = {
            "contract": "eidolon.device-foundation.claim-grant-collection",
            "enrollment_id": created.enrollment_id,
            "proposal_revision": 1,
            "collection_challenge": created.collection_challenge,
        }
        collect_response = await client.post(
            f"/api/admission/v1/enrollments/{created.enrollment_id}/claim-grants:collect",
            json={
                "command_id": "http_collect_01",
                "correlation_id": "http_intent_01",
                "enrollment_id": created.enrollment_id,
                "proposal_revision": 1,
                "collection_challenge": created.collection_challenge,
                "handoff_key_proof": sign(handoff, collection_doc),
            },
        )
        assert collect_response.status_code == 200
        assert set(collect_response.json()) == set(CollectClaimGrantResult.model_fields)
        collected = CollectClaimGrantResult.model_validate(collect_response.json())
        grant = open_wire_envelope(
            handoff,
            collected.wire_envelope.model_dump(mode="json"),
            collected.wire_envelope.aad.model_dump(mode="json"),
        )

        ack_doc = {
            "contract": "eidolon.device-foundation.claim-grant-ack",
            "enrollment_id": created.enrollment_id,
            "grant_id": collected.grant_id,
            "device_ref": grant["device_ref"],
        }
        ack_response = await client.post(
            f"/api/admission/v1/enrollments/{created.enrollment_id}/claim-grants/{collected.grant_id}:ack",
            json={
                "command_id": "http_ack_01",
                "correlation_id": "http_intent_01",
                "enrollment_id": created.enrollment_id,
                "grant_id": collected.grant_id,
                "operational_key_proof": sign(operational, ack_doc),
                "stored_claim_generation": grant["device_ref"]["claim_generation"],
                "stored_trust_epoch": grant["device_ref"]["trust_epoch"],
            },
        )
        assert ack_response.status_code == 200
        assert set(ack_response.json()) == set(AckClaimGrantResult.model_fields)
        active = AckClaimGrantResult.model_validate(ack_response.json())

        revoke_response = await client.post(
            f"/api/admission/v1/claims/{active.device_ref.device_instance_id}:revoke",
            json={
                "operation": "device.claim-revocation",
                "command_id": "http_revoke_01",
                "correlation_id": "http_remove_01",
                "device_ref": active.device_ref.model_dump(mode="json"),
                "reason": "owner-removed",
            },
        )
        assert revoke_response.status_code == 200
        assert set(revoke_response.json()) == set(RevokeClaimResult.model_fields)
        RevokeClaimResult.model_validate(revoke_response.json())

        canceled_create = await client.post(
            "/api/admission/v1/enrollments",
            json={"command_id": "http_create_02", "correlation_id": "http_intent_02", **payload},
        )
        canceled_id = CreateEnrollmentResult.model_validate(canceled_create.json()).enrollment_id
        cancel_response = await client.post(
            f"/api/admission/v1/enrollments/{canceled_id}:cancel",
            json={
                "command_id": "http_cancel_01",
                "correlation_id": "http_intent_02",
                "enrollment_id": canceled_id,
                "reason": "controller-canceled",
            },
        )
        assert cancel_response.status_code == 200
        assert set(cancel_response.json()) == set(CancelEnrollmentResult.model_fields)
        CancelEnrollmentResult.model_validate(cancel_response.json())
