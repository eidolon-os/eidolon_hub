from __future__ import annotations

import asyncio
import base64
import hashlib
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
    claim_grant_ack_proof_document,
    claim_grant_collection_proof_document,
    derive_device_instance_id,
)
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from fastapi import FastAPI
from golden import golden_vector
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.memory import InMemoryDeviceDirectoryRepository
from hub.adapters.persistence.models import (
    AdmissionBaseIdentityRow,
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
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.admission import crypto as admission_crypto
from hub.admission.application import AdmissionAuthority
from hub.admission.commissioning import (
    TRUST_PROFILE_ID,
    IssuedBaseIdentityVerifier,
    base_identity_evidence_document,
    base_identity_evidence_wire,
    commissioning_voucher_claims,
    derive_voucher_signing_key,
    sign_commissioning_voucher,
)
from hub.admission.crypto import key_id
from hub.admission.domain import ActorContext, AdmissionProblem
from hub.admission.hardware_identity import derive_hardware_identity_ref
from hub.admission.http import create_admission_router
from hub.admission.persistence import SqlAdmissionStore
from hub.admission.target_app import create_admission_target_app
from hub.application.projections.device_directory import ProjectDeviceDirectory

# Tests name the device they mean; the name becomes a real device
# instance id, which is a digest of a key and never a chosen string.
_DEVICE_OTHER = named_device_instance_id("device_other")


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


def instance_id(key: ec.EllipticCurvePrivateKey) -> str:
    """Through the contract's derivation, not a second copy of it here."""

    return derive_device_instance_id(spki(key))


DEVICE_BASE_ID = "device-base-" + "b0" * 32


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def hardware_evidence_digest(evidence: str) -> str:
    """How a Proposal names the hardware evidence it carried.

    One derivation, two readers: the payload a Body sends and the test that
    asks what the Authority should have sealed into the ClaimGrant AAD.
    """

    return "sha256:" + hashlib.sha256(evidence.encode()).hexdigest()


def base_id_for(operational_key) -> str:
    """One base identity per Body, the way the Hub mints them.

    Sharing one across two keys is not a shortcut here — it is the refusal this
    design turns on, so tests that want it must ask for it by name.
    """

    return "device-base-" + hashlib.sha256(
        ("eidolon-test-base|" + spki(operational_key)).encode()
    ).hexdigest()


def identity_ref_for(operational_key) -> str:
    return derive_hardware_identity_ref(base_id_for(operational_key))
HARDWARE_IDENTITY_REF = derive_hardware_identity_ref(DEVICE_BASE_ID)
MANAGEMENT_SECRET = b"management-secret-for-tests-32-bytes"


def voucher_for(
    operational_key,
    *,
    device_base_id: str = DEVICE_BASE_ID,
    owner_domain_id: str = "owner-domain_01",
    jti: str | None = None,
    expires_at: int = 4_102_444_800,
    signing_key: bytes | None = None,
) -> tuple[str, str]:
    """Mint the one-shot voucher a Host signs during a witnessed commissioning."""

    claims = commissioning_voucher_claims(
        device_base_id=device_base_id,
        owner_domain_id=owner_domain_id,
        operational_spki_sha256=key_id(spki(operational_key)),
        jti=jti or f"jti-{next(_JTI_SEQUENCE):032d}",
        expires_at_unix=expires_at,
    )
    voucher = sign_commissioning_voucher(
        claims=claims,
        signing_key=signing_key or derive_voucher_signing_key(MANAGEMENT_SECRET),
    )
    return voucher, str(claims["jti"])


def continuation_proof(
    operational_key,
    *,
    device_base_id: str | None = None,
    owner_domain_id: str = "owner-domain_01",
    nonce: str = "continuation-nonce-0123456789",
) -> tuple[str, str]:
    """The proof a Body signs to continue one Claim lifecycle, with no Controller."""

    document = {
        "contract": "eidolon.device-foundation.enrolled-base-key-v1",
        "device_base_id": device_base_id or base_id_for(operational_key),
        "device_instance_id": instance_id(operational_key),
        "nonce": nonce,
        "owner_domain_id": owner_domain_id,
    }
    return sign(operational_key, document), nonce


def _sequence():
    value = 0
    while True:
        value += 1
        yield value


_JTI_SEQUENCE = _sequence()


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
    voucher_signing_key = derive_voucher_signing_key(MANAGEMENT_SECRET)
    authority = AdmissionAuthority(
        store=SqlAdmissionStore(database),
        clock=clock,
        ids=ids,
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=IssuedBaseIdentityVerifier(voucher_signing_key),
    )
    yield database, authority, clock, voucher_signing_key
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


def create_payload(
    handoff_key,
    operational_key,
    signing_key=None,
    manifest_document=None,
    *,
    device_base_id: str | None = None,
    voucher: tuple[str, str] | None = None,
    proof_scheme: str = "hub-issued-commissioning-voucher-v1",
) -> dict:
    device_base_id = device_base_id or base_id_for(operational_key)
    candidate_id = instance_id(operational_key)
    operational_public_key = spki(operational_key)
    evidence_document = base_identity_evidence_document(
        device_base_id=device_base_id,
        device_instance_id=candidate_id,
        operational_public_key=operational_public_key,
    )
    evidence = base_identity_evidence_wire(
        document=evidence_document, signature=sign(operational_key, evidence_document)
    )
    # The Authority does not author this; a device does, in whatever vocabulary
    # its firmware uses. The default here is this repository's own shape, and a
    # caller can pass what a real board actually sends.
    manifest_document = manifest_document or {
        "schema_version": 1,
        "title": "Box-3",
        "properties": [],
        "actions": [],
        "events": [],
        "media": [],
    }
    proof, nonce = voucher or voucher_for(
        operational_key, device_base_id=device_base_id, signing_key=signing_key
    )
    return {
        "profile_id": TRUST_PROFILE_ID,
        "device_instance_candidate_id": candidate_id,
        "requested_owner_domain_id": "owner-domain_01",
        "hardware_identity_evidence": {
            "scheme": "hub-issued-base-p256",
            "evidence": evidence,
            "evidence_digest": hardware_evidence_digest(evidence),
        },
        "commissioning_proof": {
            "scheme": proof_scheme,
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
            "public_key": operational_public_key,
        },
    }


async def create_and_approve(harness, manifest_document=None):
    """Create and approve one enrollment, and hand back what was sent.

    The payload is returned because the evidence inside it is signed with a
    randomised ECDSA nonce: a test that wants the digest the Proposal carried
    cannot rebuild the evidence, only be handed the bytes that were sent.
    """

    _database, authority, _clock, secret = harness
    handoff_key = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational_key = ec.derive_private_key(0x234567891, ec.SECP256R1())
    payload = create_payload(handoff_key, operational_key, secret, manifest_document)
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=payload,
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
    return created, decision, handoff_key, operational_key, payload


async def collect_and_ack(harness, manifest_document=None):
    database, authority, _clock, _secret = harness
    created, decision, handoff, operational, _payload = await create_and_approve(
        harness, manifest_document
    )
    collection_document = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
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
    ack_document = claim_grant_ack_proof_document(
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        device_ref=device_ref,
    )
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
    created, decision, handoff, operational, _payload = await create_and_approve(harness)
    assert decision["decision"]["target_owner_domain_id"] == "owner-domain_01"
    assert decision["decision"]["target_business_owner_id"] == "owner_01"
    proof_doc = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
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
    ack_doc = claim_grant_ack_proof_document(
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        device_ref=device_ref,
    )
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
            activated_event = await session.scalar(
                select(AdmissionOutboxRow).where(
                    AdmissionOutboxRow.event_type == "live.eidolon.device.claim-activated.v1"
                )
            )
            assert json.loads(activated_event.event_json)["data"]["business_owner_id"] == (
                "owner_01"
            )
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


async def test_claim_ack_and_replay_immediately_refresh_public_directory(harness):
    database, authority, _clock, _secret = harness
    directory = InMemoryDeviceDirectoryRepository()
    projector = ProjectDeviceDirectory(
        devices=SqlHubRepositories(database).devices,
        directory=directory,
    )
    authority.claim_directory_projector = projector.execute

    created, _decision, collected, device_ref, ack_proof, active = await collect_and_ack(harness)
    visible = await directory.get(owner_scope="owner_01", device_id=device_ref.device_instance_id)
    assert visible is not None
    assert visible.device_ref == device_ref

    replay = await authority.ack_claim_grant(
        command_id="ack_01",
        correlation_id="reply_lost",
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        operational_key_proof=ack_proof,
        stored_claim_generation=device_ref.claim_generation,
        stored_trust_epoch=device_ref.trust_epoch,
    )
    assert replay["outcome"] == "replayed"
    assert replay["occurred_at"] == active["occurred_at"]
    assert (
        await directory.get(owner_scope="owner_01", device_id=device_ref.device_instance_id)
        == visible
    )


async def test_revoked_body_rejoins_on_its_base_identity_at_generation_two(harness):
    """Removed and admitted again: same Body, next generation, old Claim fenced.

    Nothing was erased, so the device still holds the operational key its base
    identity is bound to and is the same DeviceInstance. What it does not hold
    is standing: the Owner said no once, so coming back needs another
    Controller-witnessed voucher rather than a device that keeps asking.
    """

    database, authority, clock, voucher_signing_key = harness
    (
        first_created,
        _decision,
        first_collected,
        first_ref,
        first_ack_proof,
        _active,
    ) = await collect_and_ack(harness)
    await authority.revoke_claim(
        command_id="revoke_first",
        correlation_id="remove_first",
        device_ref=first_ref,
        reason="owner-removed",
        context=actor(),
    )

    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    with pytest.raises(AdmissionProblem) as unattended:
        await authority.create_enrollment(
            command_id="create_unattended",
            correlation_id="rejoin_unattended",
            payload=create_payload(
                handoff,
                operational,
                voucher_signing_key,
                voucher=continuation_proof(operational),
                proof_scheme="enrolled-base-key-v1",
            ),
        )
    assert unattended.value.status == 403

    second_created = await authority.create_enrollment(
        command_id="create_second",
        correlation_id="rejoin_second",
        payload=create_payload(handoff, operational, voucher_signing_key),
    )
    await authority.decide_enrollment(
        command_id="decide_second",
        correlation_id="rejoin_second",
        enrollment_id=second_created["enrollment_id"],
        payload={
            "expected_proposal_revision": 1,
            "decision": "approve",
            "target_owner_domain_id": "owner-domain_01",
            "target_business_owner_id": "owner_01",
            "target_space_id": None,
            "reviewed_manifest_ref": second_created["reviewed_manifest_ref"],
            "initial_assignment_intent": None,
            "initial_capability_policy_refs": [],
        },
        context=actor(),
    )
    collection_document = claim_grant_collection_proof_document(
        enrollment_id=second_created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=second_created["collection_challenge"],
    )
    second_collected = await authority.collect_claim_grant(
        command_id="collect_second",
        correlation_id="rejoin_second",
        enrollment_id=second_created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=second_created["collection_challenge"],
        handoff_key_proof=sign(handoff, collection_document),
    )
    async with database.sessions() as session:
        second_grant = await session.get(AdmissionGrantRow, second_collected["grant_id"])
        second_ref = DeviceRef.model_validate_json(second_grant.device_ref_json)
    ack_document = claim_grant_ack_proof_document(
        enrollment_id=second_created["enrollment_id"],
        grant_id=second_collected["grant_id"],
        device_ref=second_ref,
    )
    await authority.ack_claim_grant(
        command_id="ack_second",
        correlation_id="rejoin_second",
        enrollment_id=second_created["enrollment_id"],
        grant_id=second_collected["grant_id"],
        operational_key_proof=sign(operational, ack_document),
        stored_claim_generation=second_ref.claim_generation,
        stored_trust_epoch=second_ref.trust_epoch,
    )

    assert second_ref.device_instance_id == first_ref.device_instance_id
    assert second_ref.claim_generation == first_ref.claim_generation + 1 == 2
    old_ack_replay = await authority.ack_claim_grant(
        command_id="ack_01",
        correlation_id="delayed-old-ack",
        enrollment_id=first_created["enrollment_id"],
        grant_id=first_collected["grant_id"],
        operational_key_proof=first_ack_proof,
        stored_claim_generation=first_ref.claim_generation,
        stored_trust_epoch=first_ref.trust_epoch,
    )
    assert old_ack_replay["device_ref"] == first_ref.model_dump(mode="json")
    async with database.sessions() as session:
        claim = await session.get(AdmissionClaimRow, second_ref.device_instance_id)
        assert claim.state == "active"
        assert claim.claim_generation == 2
        assert claim.hardware_identity_ref == identity_ref_for(operational)


async def test_a_claimed_body_may_still_propose_itself_at_the_next_generation(harness):
    """Holding a Claim is not, by itself, a refusal to hear a Body again.

    The mobile client tells people the opposite — that a Host will not register
    a device it already holds, so removal is the precondition for re-adding one
    — and no rule here says that. `requires_fresh_presence` fences a Body the
    Owner has already told no, a *rejected* Proposal or a *revoked* Claim; an
    active Claim is not either of those, so a continuation on the same base
    identity is heard, queued for the Owner, and on approval upserts the Claim
    in place at the next generation.

    That matters because it is the whole difference between a device whose ref
    fell behind needing a removal — which drops the Companion binding an Owner
    chose — and needing a re-enrollment the Owner simply approves. The
    `device_instance_id` is a digest of the operational key and does not move,
    so nothing keyed on the device survives less for having gone around again.
    """

    database, authority, _clock, voucher_signing_key = harness
    _created, _decision, _collected, first_ref, _proof, _active = await collect_and_ack(harness)

    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    second_created = await authority.create_enrollment(
        command_id="create_again",
        correlation_id="rejoin_while_claimed",
        payload=create_payload(
            handoff,
            operational,
            voucher_signing_key,
            voucher=continuation_proof(operational),
            proof_scheme="enrolled-base-key-v1",
        ),
    )
    await authority.decide_enrollment(
        command_id="decide_again",
        correlation_id="rejoin_while_claimed",
        enrollment_id=second_created["enrollment_id"],
        payload={
            "expected_proposal_revision": 1,
            "decision": "approve",
            "target_owner_domain_id": "owner-domain_01",
            "target_business_owner_id": "owner_01",
            "target_space_id": None,
            "reviewed_manifest_ref": second_created["reviewed_manifest_ref"],
            "initial_assignment_intent": None,
            "initial_capability_policy_refs": [],
        },
        context=actor(),
    )
    collection_document = claim_grant_collection_proof_document(
        enrollment_id=second_created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=second_created["collection_challenge"],
    )
    second_collected = await authority.collect_claim_grant(
        command_id="collect_again",
        correlation_id="rejoin_while_claimed",
        enrollment_id=second_created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=second_created["collection_challenge"],
        handoff_key_proof=sign(handoff, collection_document),
    )
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, second_collected["grant_id"])
        second_ref = DeviceRef.model_validate_json(grant.device_ref_json)
    ack_document = claim_grant_ack_proof_document(
        enrollment_id=second_created["enrollment_id"],
        grant_id=second_collected["grant_id"],
        device_ref=second_ref,
    )
    await authority.ack_claim_grant(
        command_id="ack_again",
        correlation_id="rejoin_while_claimed",
        enrollment_id=second_created["enrollment_id"],
        grant_id=second_collected["grant_id"],
        operational_key_proof=sign(operational, ack_document),
        stored_claim_generation=second_ref.claim_generation,
        stored_trust_epoch=second_ref.trust_epoch,
    )

    # Same Body, same instance id, one Claim row, next generation.
    assert second_ref.device_instance_id == first_ref.device_instance_id
    assert second_ref.claim_generation == first_ref.claim_generation + 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdmissionClaimRow)) == 1
        claim = await session.get(AdmissionClaimRow, first_ref.device_instance_id)
    assert (claim.state, claim.claim_generation) == ("active", second_ref.claim_generation)
    # And the ref the Body was carrying a moment ago now names nothing. This is
    # the state the Host answered 409 to forever; Device Control corrects it on
    # the next configuration pull rather than requiring the Owner to remove the
    # device to get out of it.
    assert first_ref.claim_generation != claim.claim_generation


async def test_unknown_base_identity_cannot_self_enroll_with_continuation(harness):
    """A self-signed continuation is possession evidence, not standing.

    No voucher means no Controller witnessed this identity.  Accepting the
    request and creating its binding in the same transaction would reduce
    commissioning to "generate a key and name yourself".
    """

    database, authority, _clock, voucher_signing_key = harness
    handoff = ec.derive_private_key(0x567891234, ec.SECP256R1())
    operational = ec.derive_private_key(0x678912345, ec.SECP256R1())

    with pytest.raises(AdmissionProblem) as refused:
        await authority.create_enrollment(
            command_id="create_unknown_continuation",
            correlation_id="unknown_continuation",
            payload=create_payload(
                handoff,
                operational,
                voucher_signing_key,
                voucher=continuation_proof(operational),
                proof_scheme="enrolled-base-key-v1",
            ),
        )

    assert refused.value.status == 401
    async with database.sessions() as session:
        assert await session.get(AdmissionBaseIdentityRow, base_id_for(operational)) is None
        proposals = list((await session.scalars(select(AdmissionProposalRow))).all())
        assert proposals == []


async def test_a_voucher_is_spent_once_by_the_key_it_names_and_never_again(harness):
    """F-018. The four ways to try to reuse a witnessed commissioning.

    A voucher is the whole of what a Controller's presence produces, so every
    one of these would otherwise convert one moment of presence into standing
    that keeps working: replaying it, handing it to another board, waiting for
    a restart to forget it, or asking for a second lineage on one key.

    The restart is the one worth spelling out. The spent-jti ledger has to be
    durable or the refusal is only as good as this process's uptime — and a
    Hub restart is not an event a device can be prevented from waiting for.
    """

    database, authority, _clock, voucher_signing_key = harness
    handoff = ec.derive_private_key(0x1AB2C3D4E, ec.SECP256R1())
    operational = ec.derive_private_key(0x2BC3D4E5F, ec.SECP256R1())
    base_id = base_id_for(operational)
    proof = voucher_for(operational, device_base_id=base_id, signing_key=voucher_signing_key)

    accepted = await authority.create_enrollment(
        command_id="spend_once",
        correlation_id="f018",
        payload=create_payload(handoff, operational, voucher=proof),
    )
    assert accepted["enrollment_id"]

    # 1. The same voucher again, by the same Body. A different command id, so
    #    nothing can be mistaken for idempotent replay of the first request.
    with pytest.raises(AdmissionProblem) as replayed:
        await authority.create_enrollment(
            command_id="spend_twice",
            correlation_id="f018-replay",
            payload=create_payload(handoff, operational, voucher=proof),
        )
    assert replayed.value.status == 401

    # 2. Another board redeeming a voucher issued for someone else's key. The
    #    signature is ours and still valid; the key it names is not this one.
    stranger = ec.derive_private_key(0x3CD4E5F60, ec.SECP256R1())
    with pytest.raises(AdmissionProblem) as borrowed:
        await authority.create_enrollment(
            command_id="spend_borrowed",
            correlation_id="f018-borrowed",
            payload=create_payload(
                handoff,
                stranger,
                device_base_id=base_id,
                voucher=voucher_for(
                    operational, device_base_id=base_id, signing_key=voucher_signing_key
                ),
            ),
        )
    assert borrowed.value.status == 401

    # 3. The same voucher after this Hub is restarted. A fresh Authority over
    #    the same database is what a restart leaves behind: no in-process
    #    memory of the request, the ledger row still there.
    restarted = AdmissionAuthority(
        store=SqlAdmissionStore(database),
        clock=FixedClock(),
        ids=SequenceIds(),
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=IssuedBaseIdentityVerifier(voucher_signing_key),
    )
    with pytest.raises(AdmissionProblem) as after_restart:
        await restarted.create_enrollment(
            command_id="spend_after_restart",
            correlation_id="f018-restart",
            payload=create_payload(handoff, operational, voucher=proof),
        )
    assert after_restart.value.status == 401

    # 4. A second lineage for one key. Even with a Controller present and a
    #    correctly signed voucher, the binding is one to one: this key already
    #    has an identity, and a Body that could hold two could be two.
    second_base_id = base_id_for(ec.derive_private_key(0x4DE5F6071, ec.SECP256R1()))
    with pytest.raises(AdmissionProblem) as second_lineage:
        await authority.create_enrollment(
            command_id="spend_second_lineage",
            correlation_id="f018-second-lineage",
            payload=create_payload(
                handoff,
                operational,
                device_base_id=second_base_id,
                voucher=voucher_for(
                    operational,
                    device_base_id=second_base_id,
                    signing_key=voucher_signing_key,
                ),
            ),
        )
    assert second_lineage.value.status == 401

    async with database.sessions() as session:
        bound = list((await session.scalars(select(AdmissionBaseIdentityRow))).all())
        assert [row.device_base_id for row in bound] == [base_id]
        proposals = list((await session.scalars(select(AdmissionProposalRow))).all())
        assert len(proposals) == 1


async def test_erased_body_arrives_as_a_new_base_identity_and_inherits_nothing(harness):
    """Erasing local storage ends the lineage instead of continuing it.

    The factory secret that used to survive an erase is gone, so a wiped device
    cannot prove it is the same board — and a platform that recognised it anyway
    would be inheriting ownership from an unauthenticated MAC. It is a new Body,
    and the Owner is asked as if it were.
    """

    database, authority, _clock, voucher_signing_key = harness
    (_created, _decision, _collected, first_ref, _proof, _active) = await collect_and_ack(harness)
    await authority.revoke_claim(
        command_id="revoke_first",
        correlation_id="remove_first",
        device_ref=first_ref,
        reason="owner-removed",
        context=actor(),
    )

    erased_handoff = ec.derive_private_key(0x345678912, ec.SECP256R1())
    erased_operational = ec.derive_private_key(0x456789123, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_erased",
        correlation_id="rejoin_erased",
        payload=create_payload(erased_handoff, erased_operational, voucher_signing_key),
    )
    async with database.sessions() as session:
        proposal = await session.get(AdmissionProposalRow, created["enrollment_id"])
    assert proposal.hardware_identity_ref == identity_ref_for(erased_operational)
    assert proposal.hardware_identity_ref != identity_ref_for(
        ec.derive_private_key(0x234567891, ec.SECP256R1())
    )


async def test_a_second_key_may_not_present_an_already_bound_base_identity(harness):
    """One base identity, one operational key, checked at the write.

    Two keys under one identity would let a Body hand its lineage to another
    Body: the anti-rollback fence hangs off this pair, and a Claim generation
    that can be inherited is not a fence.
    """

    _database, authority, _clock, voucher_signing_key = harness
    first = ec.derive_private_key(0x234567891, ec.SECP256R1())
    await authority.create_enrollment(
        command_id="create_first",
        correlation_id="intent_first",
        payload=create_payload(ec.derive_private_key(0x123456789, ec.SECP256R1()), first),
    )
    impostor = ec.derive_private_key(0x456789123, ec.SECP256R1())
    with pytest.raises(AdmissionProblem) as refused:
        await authority.create_enrollment(
            command_id="create_impostor",
            correlation_id="intent_impostor",
            payload=create_payload(
                ec.derive_private_key(0x345678912, ec.SECP256R1()),
                impostor,
                voucher_signing_key,
                device_base_id=base_id_for(first),
            ),
        )
    assert refused.value.status == 401


async def test_the_host_asks_by_key_because_that_is_what_a_device_can_prove(harness):
    """Before signing a voucher the Host asks, and it asks about the key.

    A device presents an operational key it holds and nothing else it could be
    asked to prove. Resolving its identity from that key is what leaves the
    device with no say in what it is called: there is no value it can send that
    would have to be disbelieved.
    """

    _database, authority, _clock, voucher_signing_key = harness
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    unknown = await authority.base_identity_for_key(
        operational_key_id=key_id(spki(operational)), context=actor()
    )
    assert unknown["device_base_id"] is None

    await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(
            ec.derive_private_key(0x123456789, ec.SECP256R1()),
            operational,
            voucher_signing_key,
        ),
    )
    answered = await authority.base_identity_for_key(
        operational_key_id=key_id(spki(operational)), context=actor()
    )
    assert answered["device_base_id"] == base_id_for(operational)
    assert answered["requires_fresh_presence"] is False

    # Another key gets its own answer, never this one's.
    impostor = ec.derive_private_key(0x456789123, ec.SECP256R1())
    for_impostor = await authority.base_identity_for_key(
        operational_key_id=key_id(spki(impostor)), context=actor()
    )
    assert for_impostor["device_base_id"] is None


async def test_two_complete_lifecycles_keep_idempotency_scoped_to_each_device_ref(
    harness,
):
    """I-016: an old lifecycle's commands cannot consume the next one's work."""

    database, authority, clock, voucher_signing_key = harness
    refs: list[DeviceRef] = []
    terminal_results: list[dict] = []

    # One Body living two lifecycles. Nothing was erased between them, so the
    # operational key — and therefore the DeviceInstance — is the same one; what
    # changes is the generation, which is exactly what the fence is made of.
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    for lifecycle, handoff_seed in enumerate((0x123456789, 0x345678912), start=1):
        handoff = ec.derive_private_key(handoff_seed, ec.SECP256R1())
        command = f"lifecycle_{lifecycle}"
        payload = create_payload(handoff, operational, voucher_signing_key)
        created = await authority.create_enrollment(
            command_id=f"create_{command}", correlation_id=command, payload=payload
        )
        replayed_create = await authority.create_enrollment(
            command_id=f"create_{command}", correlation_id=f"{command}_retry", payload=payload
        )
        assert replayed_create["enrollment_id"] == created["enrollment_id"]

        decided = await authority.decide_enrollment(
            command_id=f"decide_{command}",
            correlation_id=command,
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
        collection_document = claim_grant_collection_proof_document(
            enrollment_id=created["enrollment_id"],
            proposal_revision=1,
            collection_challenge=created["collection_challenge"],
        )
        collected = await authority.collect_claim_grant(
            command_id=f"collect_{command}",
            correlation_id=command,
            enrollment_id=created["enrollment_id"],
            proposal_revision=1,
            collection_challenge=created["collection_challenge"],
            handoff_key_proof=sign(handoff, collection_document),
        )
        async with database.sessions() as session:
            grant = await session.get(AdmissionGrantRow, decided["grant_id"])
            ref = DeviceRef.model_validate_json(grant.device_ref_json)
        ack_document = claim_grant_ack_proof_document(
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            device_ref=ref,
        )
        ack_proof = sign(operational, ack_document)
        acked = await authority.ack_claim_grant(
            command_id=f"ack_{command}",
            correlation_id=command,
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            operational_key_proof=ack_proof,
            stored_claim_generation=ref.claim_generation,
            stored_trust_epoch=ref.trust_epoch,
        )
        replayed_ack = await authority.ack_claim_grant(
            command_id=f"ack_{command}",
            correlation_id=f"{command}_ack_retry",
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            operational_key_proof=ack_proof,
            stored_claim_generation=ref.claim_generation,
            stored_trust_epoch=ref.trust_epoch,
        )
        assert replayed_ack["occurred_at"] == acked["occurred_at"]

        revoked = await authority.revoke_claim(
            command_id=f"revoke_{command}",
            correlation_id=command,
            device_ref=ref,
            reason="owner-removed",
            context=actor(),
        )
        replayed_revoke = await authority.revoke_claim(
            command_id=f"revoke_{command}",
            correlation_id=f"{command}_revoke_retry",
            device_ref=ref,
            reason="owner-removed",
            context=actor(),
        )
        assert replayed_revoke["occurred_at"] == revoked["occurred_at"]
        refs.append(ref)
        terminal_results.append(revoked)
        clock.value += timedelta(seconds=1)

    assert refs[0].device_instance_id == refs[1].device_instance_id
    assert [ref.claim_generation for ref in refs] == [1, 2]
    assert terminal_results[0]["occurred_at"] < terminal_results[1]["occurred_at"]
    async with database.sessions() as session:
        claims = (
            await session.scalars(
                select(AdmissionClaimRow).order_by(AdmissionClaimRow.claim_generation)
            )
        ).all()
        assert [claim.state for claim in claims] == ["revoked"]
        assert [claim.claim_generation for claim in claims] == [2]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdmissionOutboxRow)
                .where(AdmissionOutboxRow.event_type == "live.eidolon.device.claim-revoked.v1")
            )
            == 2
        )


async def test_one_thousand_replays_queries_and_terminal_commands_have_bounded_cardinality(
    harness,
):
    """J-006: volume must not turn retries into new business facts."""

    database, authority, _clock, _secret = harness
    created, _decision, collected, ref, ack_proof, active = await collect_and_ack(harness)
    revoked = await authority.revoke_claim(
        command_id="revoke_stress",
        correlation_id="stress",
        device_ref=ref,
        reason="owner-removed",
        context=actor(),
    )

    for index in range(400):
        replayed = await authority.ack_claim_grant(
            command_id="ack_01",
            correlation_id=f"stress_ack_{index}",
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            operational_key_proof=ack_proof,
            stored_claim_generation=ref.claim_generation,
            stored_trust_epoch=ref.trust_epoch,
        )
        assert replayed["occurred_at"] == active["occurred_at"]
    for _index in range(300):
        claim = await authority.get_claim(
            device_instance_id=ref.device_instance_id, context=actor()
        )
        assert claim.state == ClaimState.REVOKED
    for index in range(300):
        replayed = await authority.revoke_claim(
            command_id="revoke_stress",
            correlation_id=f"stress_revoke_{index}",
            device_ref=ref,
            reason="owner-removed",
            context=actor(),
        )
        assert replayed["occurred_at"] == revoked["occurred_at"]

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdmissionClaimRow)) == 1
        assert await session.scalar(select(func.count()).select_from(AdmissionGrantAckRow)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdmissionOutboxRow)
                .where(AdmissionOutboxRow.event_type == "live.eidolon.device.claim-revoked.v1")
            )
            == 1
        )


async def test_persisted_hardware_identity_is_derived_and_repeats_no_device_claim(harness):
    """A Proposal records a derived identity, never the device's words.

    A hand-filled registry entry once said "hardware-box3-1cdbd47aef0c" for a
    Waveshare board, and every later generation of that Claim repeated it. The
    Authority derives this from the base identity it issued, so nothing the
    device said about itself can become permanent history.
    """

    database, authority, _clock, secret = harness
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(
            ec.derive_private_key(0x123456789, ec.SECP256R1()),
            operational,
            secret,
        ),
    )
    async with database.sessions() as session:
        proposal = await session.get(AdmissionProposalRow, created["enrollment_id"])

    assert proposal.hardware_identity_ref == identity_ref_for(operational)
    assert "box" not in proposal.hardware_identity_ref


async def test_projection_failure_replays_committed_claim_and_converges(harness):
    database, authority, _clock, _secret = harness
    directory = InMemoryDeviceDirectoryRepository()
    projector = ProjectDeviceDirectory(
        devices=SqlHubRepositories(database).devices,
        directory=directory,
    )
    attempts = 0

    async def flaky_project(device_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("projection unavailable")
        return await projector.execute(device_id)

    authority.claim_directory_projector = flaky_project
    created, _decision, handoff, operational, _payload = await create_and_approve(harness)
    collection_document = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
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
    ack_document = claim_grant_ack_proof_document(
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        device_ref=device_ref,
    )
    ack_proof = sign(operational, ack_document)
    with pytest.raises(AdmissionProblem) as unavailable:
        await authority.ack_claim_grant(
            command_id="ack_01",
            correlation_id="intent_01",
            enrollment_id=created["enrollment_id"],
            grant_id=collected["grant_id"],
            operational_key_proof=ack_proof,
            stored_claim_generation=device_ref.claim_generation,
            stored_trust_epoch=device_ref.trust_epoch,
        )
    assert unavailable.value.code == "AUTHORITY_UNAVAILABLE"
    async with database.sessions() as session:
        claim = await session.get(AdmissionClaimRow, device_ref.device_instance_id)
        assert claim is not None and claim.state == "active"

    replay = await authority.ack_claim_grant(
        command_id="ack_01",
        correlation_id="reply_lost",
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        operational_key_proof=ack_proof,
        stored_claim_generation=device_ref.claim_generation,
        stored_trust_epoch=device_ref.trust_epoch,
    )
    assert replay["outcome"] == "replayed"
    assert attempts == 2
    visible = await directory.get(owner_scope="owner_01", device_id=device_ref.device_instance_id)
    assert visible is not None and visible.device_ref == device_ref


async def test_same_command_different_payload_conflicts_and_invalid_proof_rolls_back(harness):
    database, authority, _clock, _secret = harness
    created, _decision, _handoff, _operational, _payload = await create_and_approve(harness)
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
    created, _decision, handoff, _operational, _payload = await create_and_approve(harness)
    proof_doc = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
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


async def test_hardware_evidence_its_own_digest_does_not_name_is_refused_and_costs_nothing(
    harness,
):
    """The digest is checked here or nowhere, and refusing it must not burn the voucher.

    Nothing downstream recomputes it: `collect_claim_grant` seals whatever digest
    the Proposal stored, and `ClaimGrantAAD.assert_matches_grant` compares the AAD
    against the opened ClaimGrant, which carries no hardware evidence at all. A
    digest admitted here is one a device is later asked to trust as the name of
    the evidence it sent.

    The refusal is also the one a Body can act on. It happens before the
    transaction, so the one-shot commissioning voucher is still unspent and the
    same Body may retry — which is checked by retrying, not by reading the guard.
    """

    database, authority, _clock, secret = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    payload = create_payload(handoff, operational, secret)
    # A well-formed digest of other bytes, which is what a stale or swapped one
    # looks like. A malformed string would be refused by its shape alone.
    payload["hardware_identity_evidence"]["evidence_digest"] = hardware_evidence_digest(
        payload["hardware_identity_evidence"]["evidence"] + " "
    )
    with pytest.raises(AdmissionProblem) as mismatched:
        await authority.create_enrollment(
            command_id="create_bad_evidence_digest",
            correlation_id="intent_bad_evidence_digest",
            payload=payload,
        )
    assert mismatched.value.code == "INVALID_ARGUMENT"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdmissionProposalRow)) == 0

    payload["hardware_identity_evidence"]["evidence_digest"] = hardware_evidence_digest(
        payload["hardware_identity_evidence"]["evidence"]
    )
    created = await authority.create_enrollment(
        command_id="create_after_bad_evidence_digest",
        correlation_id="intent_bad_evidence_digest",
        payload=payload,
    )
    async with database.sessions() as session:
        proposal = await session.get(AdmissionProposalRow, created["enrollment_id"])
    assert proposal.hardware_evidence_digest == hardware_evidence_digest(
        payload["hardware_identity_evidence"]["evidence"]
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
    created, decision, handoff, _operational, _payload = await create_and_approve(harness)
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
    proof_doc = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
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
    created, decision, _handoff, _operational, _payload = await create_and_approve(harness)
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
            command_id="create_rollback",
            correlation_id="intent_rollback",
            # A second attempt by the same Body carries a second voucher: one
            # is spent at the first attempt and never spends again.
            payload=create_payload(handoff, operational, secret),
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
        command_id="create_cancel",
        correlation_id="intent_cancel",
        payload=create_payload(handoff, operational, secret),
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
        command_id="create_expire",
        correlation_id="intent_expire",
        payload=create_payload(handoff, operational, secret),
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
    created, _decision, handoff, _operational, payload = await create_and_approve(harness)
    proof_doc = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
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
    # Which members exist is the contract's statement, not this test's: the
    # vector every implementation canonicalises is what says the set is whole.
    assert set(envelope["aad"]) == set(golden_vector("claim-grant-aad.json")["aad"])
    assert envelope["aad"] == {
        "contract": "eidolon.device-foundation.claim-grant-aad",
        "profile_id": "eidolon-trust-p256-hpke-v1",
        "enrollment_id": created["enrollment_id"],
        "proposal_revision": 1,
        "device_instance_id": instance_id(_operational),
        # The member nothing else reaches. `ClaimGrantAAD.assert_matches_grant`
        # compares the AAD against the opened ClaimGrant, and the ClaimGrant
        # carries no hardware evidence — so this is the only place the seal is
        # held to the evidence the Proposal was created with. Named from that
        # evidence rather than from the digest sent beside it, so a stale, empty
        # or another device's digest is a failure here and not a passing echo.
        "hardware_evidence_digest": hardware_evidence_digest(
            payload["hardware_identity_evidence"]["evidence"]
        ),
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
    tampered_aad = {**envelope["aad"], "device_instance_id": _DEVICE_OTHER}
    with pytest.raises(InvalidTag):
        open_wire_envelope(handoff, envelope, tampered_aad)


async def test_claim_stream_dataschema_cursor_replay_publish_failure_and_restart(harness):
    database, authority, _clock, _secret = harness
    _created, _decision, _collected, device_ref, _proof, _active = await collect_and_ack(harness)
    first = await authority.claim_event_page(
        cursor=ClaimEventCursor(stream_position=0),
        limit=100,
        owner_domain_id=OwnerDomainId("owner-domain_01"),
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
        cursor=ClaimEventCursor(stream_position=0),
        limit=100,
        owner_domain_id=OwnerDomainId("owner-domain_01"),
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
        cursor=ClaimEventCursor(stream_position=1),
        limit=100,
        owner_domain_id=OwnerDomainId("owner-domain_01"),
    )
    assert [item.stream_position for item in resumed.events] == [2]
    with pytest.raises(AdmissionProblem, match="ahead") as gap:
        await restarted.claim_event_page(
            cursor=ClaimEventCursor(stream_position=99),
            limit=100,
            owner_domain_id=OwnerDomainId("owner-domain_01"),
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


async def test_a_removed_body_is_refused_in_words_the_contract_can_carry(harness):
    """The refusal a Body meets when it asks again has to survive the wire.

    It did not. The guard fired, and then the response could not be built: the
    problem carried a category the frozen contract has no name for, so a Body
    that had been removed was told 500 by a Hub that had in fact decided 403.
    Found on hardware, where the difference is the whole message — one says
    "ask a person", the other says "the Host is broken".
    """

    _database, authority, _clock, voucher_signing_key = harness
    handoff = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_01",
        correlation_id="intent_01",
        payload=create_payload(handoff, operational, voucher_signing_key),
    )
    await authority.decide_enrollment(
        command_id="decide_01",
        correlation_id="intent_01",
        enrollment_id=created["enrollment_id"],
        payload={
            "expected_proposal_revision": 1,
            "decision": "reject",
            "target_owner_domain_id": "owner-domain_01",
            "target_business_owner_id": "owner_01",
            "target_space_id": None,
            "reviewed_manifest_ref": created["reviewed_manifest_ref"],
            "initial_assignment_intent": None,
            "initial_capability_policy_refs": [],
        },
        context=actor(),
    )

    async def actor_provider(_request):
        return actor()

    app = create_admission_target_app(authority=authority, actor_provider=actor_provider)
    transport = httpx.ASGITransport(app=app)
    payload = create_payload(
        handoff,
        operational,
        voucher_signing_key,
        voucher=continuation_proof(operational),
        proof_scheme="enrolled-base-key-v1",
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/admission/v1/enrollments",
            json={"command_id": "create_again", "correlation_id": "intent_again", **payload},
        )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "FORBIDDEN"
    assert "new commissioning" in response.json()["detail"]
    assert set(response.json()) == set(DeviceProblem.model_fields)
    DeviceProblem.model_validate(response.json())


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

        collection_doc = claim_grant_collection_proof_document(
            enrollment_id=created.enrollment_id,
            proposal_revision=1,
            collection_challenge=created.collection_challenge,
        )
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

        ack_doc = claim_grant_ack_proof_document(
            enrollment_id=created.enrollment_id,
            grant_id=collected.grant_id,
            device_ref=DeviceRef.model_validate(grant["device_ref"]),
        )
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
            json={
                "command_id": "http_create_02",
                "correlation_id": "http_intent_02",
                **create_payload(handoff, operational, secret),
            },
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


async def test_only_an_approver_can_see_what_is_waiting_to_be_approved(harness) -> None:
    """The pending queue is the approver's view, and one rule says so.

    A Proposal nobody has decided yet is not attributable to a business Owner,
    so `get_enrollment_recovery` shows it only to a principal holding
    `device.claim.approve`. The page has to agree: a Controller that could read
    the queue but not the Enrollments in it would see a list it cannot open.
    """

    _database, authority, _clock, secret = harness
    handoff_key = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational_key = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_scope_01",
        correlation_id="intent_scope_01",
        payload=create_payload(handoff_key, operational_key, secret),
    )
    reader = ActorContext(
        actor=ControllerActorRef(
            principal_id="controller_reader",
            owner_domain_id=OwnerDomainId("owner-domain_01"),
            granted_scopes=("device.read",),
            authentication_strength="software",
        ),
        owner_domain_id=OwnerDomainId("owner-domain_01"),
        business_owner_id=BusinessOwnerId("owner_01"),
    )
    query = EnrollmentProposalQuery(
        owner_domain_id="owner-domain_01",
        states=(EnrollmentProposalState.PENDING_REVIEW,),
        cursor=None,
        limit=50,
    )

    with pytest.raises(AdmissionProblem) as page_refusal:
        await authority.list_enrollment_recovery(query=query, context=reader)
    assert page_refusal.value.status == 403

    with pytest.raises(AdmissionProblem) as read_refusal:
        await authority.get_enrollment_recovery(
            enrollment_id=created["enrollment_id"], context=reader
        )
    assert read_refusal.value.status == 404

    page = await authority.list_enrollment_recovery(query=query, context=actor())
    assert [item.proposal.enrollment_id for item in page.items] == [created["enrollment_id"]]
    opened = await authority.get_enrollment_recovery(
        enrollment_id=created["enrollment_id"], context=actor()
    )
    assert opened.proposal.enrollment_id == created["enrollment_id"]


async def test_collecting_before_a_decision_says_a_decision_is_required(harness) -> None:
    """The normal wait of a device nobody has approved yet must read as one.

    An undecided Proposal has no expected revision, so comparing revisions
    first could only fail — and it answered REVISION_CONFLICT, which a device
    reads as terminal for this Proposal. It is not terminal; it is the state
    every device passes through between being plugged in and being approved.
    """

    _database, authority, _clock, secret = harness
    handoff_key = ec.derive_private_key(0x123456789, ec.SECP256R1())
    operational_key = ec.derive_private_key(0x234567891, ec.SECP256R1())
    created = await authority.create_enrollment(
        command_id="create_wait_01",
        correlation_id="intent_wait_01",
        payload=create_payload(handoff_key, operational_key, secret),
    )

    with pytest.raises(AdmissionProblem) as waiting:
        await authority.collect_claim_grant(
            command_id="collect_wait_01",
            correlation_id="intent_wait_01",
            enrollment_id=created["enrollment_id"],
            proposal_revision=created["proposal_revision"],
            collection_challenge=created["collection_challenge"],
            handoff_key_proof=sign(
                handoff_key,
                claim_grant_collection_proof_document(
                    enrollment_id=created["enrollment_id"],
                    proposal_revision=created["proposal_revision"],
                    collection_challenge=created["collection_challenge"],
                ),
            ),
        )

    assert waiting.value.code == "DECISION_REQUIRED"
    assert waiting.value.status == 409


async def test_a_claimed_device_can_be_hydrated_into_the_owner_directory(harness) -> None:
    """The Authority must survive reading back what it just admitted.

    The owner-facing directory hydrates every row at startup. The first device
    ever claimed canonically sent a Manifest with no `schema_version` — a field
    that belongs to Hub's own manifest vocabulary, not to the canonical one —
    so Hub stored a document it could not decode and crash-looped on boot,
    admitting nothing and answering nothing.
    """

    database, _authority, _clock, _secret = harness
    _created, _decision, _collected, device_ref, _proof, _active = await collect_and_ack(
        harness, {"endpoints": []}
    )

    repositories = SqlHubRepositories(database)
    directory = InMemoryDeviceDirectoryRepository()
    projected = await ProjectDeviceDirectory(
        devices=repositories.devices, directory=directory
    ).execute_all()

    assert [entry.device_id for entry in projected] == [device_ref.device_instance_id]
    assert projected[0].lifecycle_state == "approved"
