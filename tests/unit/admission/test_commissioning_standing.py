"""Standing recorded at voucher issuance decides the Proposal that carries it.

A device is admitted by its Owner at one moment: a Controller this Owner
accepted, physically present at the device, asks this Host to sign a voucher
for the key the device just showed it. Everything after that is the device
carrying the Owner's decision to the Authority. The Authority used to receive
it as an anonymous proof and ask the Owner to decide again, from a queue with
a fifteen-minute clock; a phone that could not reach the Host inside that
window left a device admitted by its Owner and refused by its Host.

These tests hold the Authority to the other reading: the asking before the
signing *is* the decision, recorded under the voucher's ``jti`` by the
Controller that made it, and applied to the Proposal when it arrives — by that
Controller, on the record. What has no standing still waits for the queue.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from eidolon_sdk.device_foundation.v1 import (
    BusinessOwnerId,
    ControllerActorRef,
    DeviceRef,
    OwnerDomainId,
    claim_grant_ack_proof_document,
    claim_grant_collection_proof_document,
    derive_voucher_signing_key,
)
from sqlalchemy import select
from test_admission_authority import (
    MANAGEMENT_SECRET,
    FixedClock,
    SequenceIds,
    actor,
    base_id_for,
    continuation_proof,
    create_payload,
    sign,
    spki,
    voucher_for,
)

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    AdmissionCommissioningStandingRow,
    AdmissionDecisionRow,
    AdmissionGrantRow,
    AdmissionOutboxRow,
    AdmissionProposalRow,
)
from hub.admission.application import AdmissionAuthority
from hub.admission.commissioning import IssuedBaseIdentityVerifier
from hub.admission.crypto import key_id
from hub.admission.domain import ActorContext, AdmissionProblem
from hub.admission.persistence import SqlAdmissionStore
from hub.admission.target_app import create_admission_target_app

HANDOFF = 0x123456789
OPERATIONAL = 0x234567891


def keys():
    return (
        ec.derive_private_key(HANDOFF, ec.SECP256R1()),
        ec.derive_private_key(OPERATIONAL, ec.SECP256R1()),
    )


def vouch(operational, *, jti: str, signing_key: bytes) -> tuple[str, str]:
    """A voucher for the identity the evidence will name, as a Host would sign it."""

    return voucher_for(
        operational, device_base_id=base_id_for(operational), jti=jti, signing_key=signing_key
    )


async def record(authority, operational, *, jti: str, context: ActorContext | None = None):
    return await authority.record_commissioning_standing(
        jti=jti,
        operational_key_id=key_id(spki(operational)),
        context=context or actor(),
    )


async def proposal_row(database, enrollment_id: str):
    async with database.sessions() as session:
        return await session.get(AdmissionProposalRow, enrollment_id)


async def decision_row(database, enrollment_id: str):
    async with database.sessions() as session:
        return await session.scalar(
            select(AdmissionDecisionRow).where(AdmissionDecisionRow.enrollment_id == enrollment_id)
        )


async def collect(authority, created, handoff, *, command_id: str):
    document = claim_grant_collection_proof_document(
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
    )
    return await authority.collect_claim_grant(
        command_id=command_id,
        correlation_id=command_id,
        enrollment_id=created["enrollment_id"],
        proposal_revision=1,
        collection_challenge=created["collection_challenge"],
        handoff_key_proof=sign(handoff, document),
    )


async def test_the_standing_recorded_at_issuance_decides_the_proposal_it_vouches_for(harness):
    database, authority, _clock, signing_key = harness
    handoff, operational = keys()

    answered = await record(authority, operational, jti="jti-standing-01")
    # The same answer the Host used to get by asking for the identity alone.
    assert answered["device_base_id"] is None
    assert answered["requires_fresh_presence"] is False
    assert answered["jti"] == "jti-standing-01"

    created = await authority.create_enrollment(
        command_id="create_standing",
        correlation_id="intent_standing",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=vouch(operational, jti="jti-standing-01", signing_key=signing_key),
        ),
    )
    # What the device is told is the Proposal as created: that is what it
    # collects against, and the firmware holds the create result to it.
    assert created["state"] == "pending_review"
    assert created["proposal_revision"] == 1

    proposal = await proposal_row(database, created["enrollment_id"])
    assert proposal.state == "approved_awaiting_handoff"
    assert proposal.revision == 2
    decision = await decision_row(database, created["enrollment_id"])
    assert decision.decision == "approve"
    decided_by = ControllerActorRef.model_validate_json(decision.actor_json)
    assert decided_by.principal_id == "controller_01"
    assert decision.target_business_owner_id == "owner_01"
    assert decision.expected_proposal_revision == 1

    async with database.sessions() as session:
        events = (
            await session.scalars(
                select(AdmissionOutboxRow.event_type)
                .where(AdmissionOutboxRow.aggregate_id == created["enrollment_id"])
                .order_by(AdmissionOutboxRow.aggregate_revision)
            )
        ).all()
    assert events == [
        "live.eidolon.device.enrollment-proposal-created.v1",
        "live.eidolon.device.enrollment-approved.v1",
    ]

    # The device's very next request already finds its Grant.
    collected = await collect(authority, created, handoff, command_id="collect_standing")
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, collected["grant_id"])
        device_ref = DeviceRef.model_validate_json(grant.device_ref_json)
    assert grant.decision_id == decision.decision_id
    ack_document = claim_grant_ack_proof_document(
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        device_ref=device_ref,
    )
    active = await authority.ack_claim_grant(
        command_id="ack_standing",
        correlation_id="intent_standing",
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        operational_key_proof=sign(operational, ack_document),
        stored_claim_generation=device_ref.claim_generation,
        stored_trust_epoch=device_ref.trust_epoch,
    )
    assert active["device_ref"]["device_instance_id"] == device_ref.device_instance_id


async def test_without_recorded_standing_the_proposal_waits_for_the_queue(harness):
    database, authority, _clock, signing_key = harness
    handoff, operational = keys()
    created = await authority.create_enrollment(
        command_id="create_queue",
        correlation_id="intent_queue",
        payload=create_payload(handoff, operational, signing_key),
    )
    proposal = await proposal_row(database, created["enrollment_id"])
    assert proposal.state == "pending_review"
    assert await decision_row(database, created["enrollment_id"]) is None
    with pytest.raises(AdmissionProblem) as undecided:
        await collect(authority, created, handoff, command_id="collect_queue")
    assert undecided.value.code == "DECISION_REQUIRED"


async def test_a_continuation_after_expiry_is_decided_by_the_standing_its_key_earned(harness):
    """The clock ran out on the Proposal, not on the Owner's decision.

    A device whose first Proposal expired before it could collect proposes
    again on its enrolled base key, with no voucher to name a standing. The
    standing its key earned is still the Owner's word, and is what decides.
    """

    database, authority, clock, signing_key = harness
    handoff, operational = keys()
    await record(authority, operational, jti="jti-standing-02")
    first = await authority.create_enrollment(
        command_id="create_first",
        correlation_id="intent_first",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=vouch(operational, jti="jti-standing-02", signing_key=signing_key),
        ),
    )
    clock.value += timedelta(minutes=16)
    assert await authority.expire_due() == 1
    with pytest.raises(AdmissionProblem) as gone:
        await collect(authority, first, handoff, command_id="collect_late")
    assert gone.value.code == "PROPOSAL_EXPIRED"

    again = await authority.create_enrollment(
        command_id="create_again",
        correlation_id="intent_again",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=continuation_proof(operational),
            proof_scheme="enrolled-base-key-v1",
        ),
    )
    proposal = await proposal_row(database, again["enrollment_id"])
    assert proposal.state == "approved_awaiting_handoff"
    decision = await decision_row(database, again["enrollment_id"])
    assert ControllerActorRef.model_validate_json(decision.actor_json).principal_id == (
        "controller_01"
    )
    collected = await collect(authority, again, handoff, command_id="collect_again")
    assert collected["grant_id"]


async def test_standing_that_says_less_than_a_decision_decides_nothing(harness):
    database, authority, _clock, signing_key = harness
    handoff, operational = keys()

    # A Controller that could not decide from the queue cannot record standing.
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
    with pytest.raises(AdmissionProblem) as refused:
        await record(authority, operational, jti="jti-reader", context=reader)
    assert refused.value.code == "FORBIDDEN"

    # Standing recorded for another key does not decide this key's Proposal,
    # even when the voucher the device presents names that very jti.
    other = ec.derive_private_key(0x456789123, ec.SECP256R1())
    await record(authority, other, jti="jti-other-key")
    created = await authority.create_enrollment(
        command_id="create_mismatch",
        correlation_id="intent_mismatch",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=vouch(operational, jti="jti-other-key", signing_key=signing_key),
        ),
    )
    proposal = await proposal_row(database, created["enrollment_id"])
    assert proposal.state == "pending_review"
    assert await decision_row(database, created["enrollment_id"]) is None


async def test_recording_standing_is_idempotent_and_refuses_a_second_meaning(harness):
    database, authority, _clock, _signing_key = harness
    _handoff, operational = keys()
    first = await record(authority, operational, jti="jti-twice")
    second = await record(authority, operational, jti="jti-twice")
    assert first == second
    async with database.sessions() as session:
        rows = (await session.scalars(select(AdmissionCommissioningStandingRow))).all()
    assert [row.jti for row in rows] == ["jti-twice"]

    other = ec.derive_private_key(0x456789123, ec.SECP256R1())
    with pytest.raises(AdmissionProblem) as conflict:
        await record(authority, other, jti="jti-twice")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


async def test_deciding_from_standing_can_be_switched_off(tmp_path):
    database = HubDatabase.sqlite(
        tmp_path / "hub.sqlite3",
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
    )
    await database.initialize_schema()
    signing_key = derive_voucher_signing_key(MANAGEMENT_SECRET)
    authority = AdmissionAuthority(
        store=SqlAdmissionStore(database),
        clock=FixedClock(),
        ids=SequenceIds(),
        owner_domain_id="owner-domain_01",
        owner_domain_generation=3,
        commissioning_proofs=IssuedBaseIdentityVerifier(signing_key),
        decide_from_standing=False,
    )
    try:
        handoff, operational = keys()
        await record(authority, operational, jti="jti-off")
        created = await authority.create_enrollment(
            command_id="create_off",
            correlation_id="intent_off",
            payload=create_payload(
                handoff,
                operational,
                signing_key,
                voucher=vouch(operational, jti="jti-off", signing_key=signing_key),
            ),
        )
        assert (await proposal_row(database, created["enrollment_id"])).state == "pending_review"
    finally:
        await database.close()


async def test_a_removed_body_is_decided_again_only_by_a_fresh_standing(harness):
    """Removal ends the standing; the next witnessed voucher starts another.

    The device still holds its key, so it can still propose on it; the Owner
    said no, so that Proposal is refused outright. A new voucher is a new
    moment of presence with a Controller, and the standing it records is what
    decides the Proposal that carries it — at the next generation.
    """

    database, authority, _clock, signing_key = harness
    handoff, operational = keys()
    await record(authority, operational, jti="jti-gen-1")
    created = await authority.create_enrollment(
        command_id="create_gen1",
        correlation_id="intent_gen1",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=vouch(operational, jti="jti-gen-1", signing_key=signing_key),
        ),
    )
    collected = await collect(authority, created, handoff, command_id="collect_gen1")
    async with database.sessions() as session:
        grant = await session.get(AdmissionGrantRow, collected["grant_id"])
        device_ref = DeviceRef.model_validate_json(grant.device_ref_json)
    ack_document = claim_grant_ack_proof_document(
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        device_ref=device_ref,
    )
    await authority.ack_claim_grant(
        command_id="ack_gen1",
        correlation_id="intent_gen1",
        enrollment_id=created["enrollment_id"],
        grant_id=collected["grant_id"],
        operational_key_proof=sign(operational, ack_document),
        stored_claim_generation=device_ref.claim_generation,
        stored_trust_epoch=device_ref.trust_epoch,
    )
    await authority.revoke_claim(
        command_id="revoke_gen1",
        correlation_id="remove_gen1",
        device_ref=device_ref,
        reason="owner-removed",
        context=actor(),
    )

    with pytest.raises(AdmissionProblem) as unattended:
        await authority.create_enrollment(
            command_id="create_unattended",
            correlation_id="rejoin_unattended",
            payload=create_payload(
                handoff,
                operational,
                signing_key,
                voucher=continuation_proof(operational),
                proof_scheme="enrolled-base-key-v1",
            ),
        )
    assert unattended.value.status == 403

    answered = await record(
        authority, operational, jti="jti-gen-2", context=actor(principal="controller_02")
    )
    assert answered["device_base_id"] == base_id_for(operational)
    second = await authority.create_enrollment(
        command_id="create_gen2",
        correlation_id="intent_gen2",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=vouch(operational, jti="jti-gen-2", signing_key=signing_key),
        ),
    )
    proposal = await proposal_row(database, second["enrollment_id"])
    assert proposal.state == "approved_awaiting_handoff"
    decision = await decision_row(database, second["enrollment_id"])
    assert ControllerActorRef.model_validate_json(decision.actor_json).principal_id == (
        "controller_02"
    )
    second_collected = await collect(authority, second, handoff, command_id="collect_gen2")
    async with database.sessions() as session:
        second_grant = await session.get(AdmissionGrantRow, second_collected["grant_id"])
        second_ref = DeviceRef.model_validate_json(second_grant.device_ref_json)
    assert second_ref.claim_generation == device_ref.claim_generation + 1


async def test_the_host_records_standing_over_http_and_is_answered_the_identity(harness):
    database, authority, _clock, signing_key = harness
    handoff, operational = keys()

    async def actor_provider(_request):
        return actor()

    app = create_admission_target_app(authority=authority, actor_provider=actor_provider)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        recorded = await client.post(
            "/api/admission/v1/commissioning-standings",
            json={
                "contract_version": "1",
                "jti": "jti-http-01",
                "operational_key_id": key_id(spki(operational)),
            },
        )
        assert recorded.status_code == 200
        assert recorded.json() == {
            "contract_version": "1",
            "operational_key_id": key_id(spki(operational)),
            "device_base_id": None,
            "requires_fresh_presence": False,
            "jti": "jti-http-01",
        }
        stray = await client.post(
            "/api/admission/v1/commissioning-standings",
            json={"jti": "jti-http-02", "operational_key_id": key_id(spki(operational)), "x": 1},
        )
        assert stray.status_code == 422
        malformed = await client.post(
            "/api/admission/v1/commissioning-standings",
            json={"jti": "jti-http-03", "operational_key_id": "not-a-fingerprint"},
        )
        assert malformed.status_code == 422

    created = await authority.create_enrollment(
        command_id="create_http",
        correlation_id="intent_http",
        payload=create_payload(
            handoff,
            operational,
            signing_key,
            voucher=vouch(operational, jti="jti-http-01", signing_key=signing_key),
        ),
    )
    assert (await proposal_row(database, created["enrollment_id"])).state == (
        "approved_awaiting_handoff"
    )
