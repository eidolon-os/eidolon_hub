"""Canonical Proposal/Decision/Grant/Ack/Claim application service."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

import rfc8785

from hub.admission.commissioning import (
    ENROLLED_BASE_KEY_SCHEME,
    VOUCHER_SCHEME,
    CommissioningProofVerifier,
    VerifiedCommissioning,
)
from hub.admission.crypto import key_id, seal_claim_grant, verify_p256_proof
from hub.admission.domain import (
    ActorContext,
    AdmissionProblem,
    fingerprint,
    require_transition,
)
from hub.admission.persistence import SqlAdmissionStore, aware
from hub.contracts.bindings.admission import (
    AdmissionListCursor,
    ApprovalDecision,
    BusinessOwnerId,
    ClaimActivatedEvent,
    ClaimEventCursor,
    ClaimEventPage,
    ClaimEventStreamItem,
    ClaimGrant,
    ClaimGrantAAD,
    ClaimPage,
    ClaimQuery,
    ClaimRecord,
    ClaimRevokedEvent,
    ClaimState,
    DeviceRef,
    EnrollmentProposal,
    EnrollmentProposalPage,
    EnrollmentProposalQuery,
    EnrollmentProposalState,
    EnrollmentRecoveryProjection,
    GrantDeliveryRecord,
    ManifestRef,
    OwnerDomainId,
    derive_device_instance_id,
)
from hub.ports.identity import Clock, IdGenerator


class ClaimDirectoryProjector(Protocol):
    async def __call__(self, device_instance_id: str) -> object: ...


class AdmissionAuthority:
    SOURCE = "urn:eidolon:authority:admission"

    def __init__(
        self,
        *,
        store: SqlAdmissionStore,
        clock: Clock,
        ids: IdGenerator,
        owner_domain_id: str,
        owner_domain_generation: int,
        commissioning_proofs: CommissioningProofVerifier,
        claim_directory_projector: ClaimDirectoryProjector | None = None,
        proposal_ttl: timedelta = timedelta(minutes=15),
        grant_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.owner_domain_id = OwnerDomainId(owner_domain_id)
        self.owner_domain_generation = owner_domain_generation
        self.commissioning_proofs = commissioning_proofs
        self.claim_directory_projector = claim_directory_projector
        self.proposal_ttl = proposal_ttl
        self.grant_ttl = grant_ttl

    async def _project_committed_claim(self, result: dict) -> None:
        if self.claim_directory_projector is None:
            return
        try:
            device_ref = DeviceRef.model_validate(result["device_ref"])
            await self.claim_directory_projector(device_ref.device_instance_id)
        except Exception as exc:
            raise AdmissionProblem(
                "AUTHORITY_UNAVAILABLE",
                "Claim committed but public directory projection is unavailable",
                status=503,
                category="unavailable",
                retryable=True,
            ) from exc

    @staticmethod
    def _event(
        *,
        event_id: str,
        event_type: str,
        subject: str,
        owner_domain_id: str,
        revision: int,
        command_id: str,
        correlation_id: str,
        occurred_at,
        data: dict,
    ) -> dict:
        dataschemas = {
            "live.eidolon.device.claim-activated.v1": "https://contracts.eidolon.live/device-foundation/v1/events/claim-activated-data.schema.json",
            "live.eidolon.device.claim-revoked.v1": "https://contracts.eidolon.live/device-foundation/v1/events/claim-revoked-data.schema.json",
        }
        return {
            "specversion": "1.0",
            "id": event_id,
            "source": AdmissionAuthority.SOURCE,
            "type": event_type,
            "subject": subject,
            "time": occurred_at.isoformat().replace("+00:00", "Z"),
            "datacontenttype": "application/json",
            "dataschema": dataschemas.get(
                event_type,
                "https://contracts.eidolon.live/device-foundation/v1/events/schemas.schema.json",
            ),
            "audience": "eidolon-claim-consumers"
            if event_type in dataschemas
            else "eidolon-admission-audit",
            "ownerdomainid": owner_domain_id,
            "aggregaterev": revision,
            "correlationid": correlation_id,
            "causationid": command_id,
            "data": data,
        }

    async def create_enrollment(
        self, *, command_id: str, correlation_id: str, payload: dict
    ) -> dict:
        requested = OwnerDomainId(payload["requested_owner_domain_id"])
        if requested != self.owner_domain_id:
            raise AdmissionProblem(
                "OWNER_DOMAIN_MISMATCH",
                "requested Owner Domain is not served here",
                status=403,
                category="forbidden",
            )
        fp = fingerprint("admission.create-enrollment", requested, payload)
        replay = await self.store.replay(
            owner_domain_id=str(requested),
            command_type="admission.create-enrollment",
            command_id=command_id,
            fingerprint=fp,
        )
        if replay is not None:
            return replay
        hardware = payload["hardware_identity_evidence"]
        actual_digest = "sha256:" + hashlib.sha256(hardware["evidence"].encode()).hexdigest()
        if actual_digest != hardware["evidence_digest"]:
            raise AdmissionProblem(
                "INVALID_ARGUMENT",
                "hardware evidence digest mismatch",
                status=422,
                category="invalid",
            )
        try:
            handoff_key_id = key_id(payload["handoff_key"]["public_key"])
            operational_key_id = key_id(payload["operational_key"]["public_key"])
            # What the candidate id must be is the contract's rule, not this
            # module's string concatenation. Derived inside the same guard as
            # the key ids because it reads the very same key.
            expected_instance_id = derive_device_instance_id(
                payload["operational_key"]["public_key"]
            )
        except ValueError as exc:
            raise AdmissionProblem(
                "INVALID_ARGUMENT", str(exc), status=422, category="invalid"
            ) from exc
        if payload["device_instance_candidate_id"] != expected_instance_id:
            raise AdmissionProblem(
                "INVALID_ARGUMENT",
                "device instance candidate is not bound to the operational key",
                status=422,
                category="invalid",
            )
        proof = payload["commissioning_proof"]
        now = self.clock.now()
        verified: VerifiedCommissioning | None = self.commissioning_proofs.verify(
            device_instance_id=payload["device_instance_candidate_id"],
            owner_domain_id=str(requested),
            nonce=proof["nonce"],
            proof=proof["proof"],
            hardware_identity_evidence=hardware,
            operational_public_key=payload["operational_key"]["public_key"],
            now_unix=int(now.timestamp()),
        )
        if verified is None:
            raise AdmissionProblem(
                "UNAUTHENTICATED", "commissioning proof is invalid", status=401, category="auth"
            )
        if verified.scheme not in (VOUCHER_SCHEME, ENROLLED_BASE_KEY_SCHEME):
            raise AdmissionProblem(
                "UNAUTHENTICATED", "commissioning proof is invalid", status=401, category="auth"
            )
        if proof["scheme"] != verified.scheme:
            # The wire scheme is what the reviewer, the audit record and every
            # later reader will believe. A proof that verified as one thing
            # while announcing another is refused rather than reconciled.
            raise AdmissionProblem(
                "UNAUTHENTICATED",
                "commissioning proof does not match the scheme it declares",
                status=401,
                category="auth",
            )
        try:
            identity_ref = verified.identity.hardware_identity_ref()
        except ValueError as exc:
            raise AdmissionProblem(
                "INVALID_ARGUMENT", str(exc), status=422, category="invalid"
            ) from exc
        manifest = payload["manifest"]
        canonical_manifest = rfc8785.dumps(manifest["document"])
        actual_manifest_digest = "sha256:" + hashlib.sha256(canonical_manifest).hexdigest()
        if manifest["digest"] != actual_manifest_digest:
            raise AdmissionProblem(
                "INVALID_ARGUMENT",
                "ManifestRef digest does not match the canonical Manifest document",
                status=422,
                category="invalid",
            )
        manifest_ref = ManifestRef(
            manifest_id=manifest["manifest_id"],
            revision=manifest["revision"],
            digest=manifest["digest"],
        )
        enrollment_id = self.ids.new("enrollment")
        challenge = secrets.token_urlsafe(32)
        result = {
            "command_id": command_id,
            "outcome": "committed",
            "enrollment_id": enrollment_id,
            "proposal_revision": 1,
            "state": "pending_review",
            "collection_challenge": challenge,
            "expires_at": (now + self.proposal_ttl).isoformat().replace("+00:00", "Z"),
            "reviewed_manifest_ref": manifest_ref.model_dump(mode="json"),
            "occurred_at": now.isoformat().replace("+00:00", "Z"),
        }
        event_id = self.ids.new("admission-event")
        event = self._event(
            event_id=event_id,
            event_type="live.eidolon.device.enrollment-proposal-created.v1",
            subject=f"enrollments/{enrollment_id}",
            owner_domain_id=str(requested),
            revision=1,
            command_id=command_id,
            correlation_id=correlation_id,
            occurred_at=now,
            data={
                "enrollment_id": enrollment_id,
                "manifest_ref": manifest_ref.model_dump(mode="json"),
            },
        )
        async with self.store.lock:
            async with self.store.transaction() as session:
                locked_replay = await self.store.replay_in_session(
                    session,
                    owner_domain_id=str(requested),
                    command_type="admission.create-enrollment",
                    command_id=command_id,
                    fingerprint=fp,
                )
                if locked_replay is not None:
                    return locked_replay
                # Standing was proved above; what remains is durable and
                # therefore belongs inside this transaction. A voucher spent
                # twice, a base identity that acquires a second key, or a
                # Revoked identity that walks back in on its own are all
                # outcomes of a write, not of a signature check.
                if verified.scheme == VOUCHER_SCHEME:
                    await self.store.consume_commissioning_voucher(
                        session,
                        jti=str(verified.jti),
                        device_base_id=verified.identity.device_base_id,
                        operational_key_id=operational_key_id,
                        expires_at=datetime.fromtimestamp(int(verified.expires_at or 0), UTC),
                        consumed_at=now,
                    )
                    await self.store.bind_base_identity(
                        session,
                        device_base_id=verified.identity.device_base_id,
                        owner_domain_id=str(requested),
                        hardware_identity_ref=identity_ref,
                        operational_key_id=operational_key_id,
                        provenance=(
                            "minted"
                            if verified.identity.device_base_id.startswith("device-base-")
                            else "derived-from-controller"
                        ),
                        bound_at=now,
                    )
                else:
                    # A continuation signature proves possession of a key; it
                    # does not prove that this Owner Domain ever issued the
                    # base identity named by the requester.  Only a previously
                    # persisted one-to-one binding supplies that standing.
                    # Creating the binding here would let any new key mint its
                    # own identity and bypass Controller-witnessed commissioning.
                    bound = await self.store.base_identity(
                        session, verified.identity.device_base_id
                    )
                    if (
                        bound is None
                        or bound.owner_domain_id != str(requested)
                        or bound.operational_key_id != operational_key_id
                        or bound.hardware_identity_ref != identity_ref
                    ):
                        raise AdmissionProblem(
                            "UNAUTHENTICATED",
                            "base identity continuation has no matching issued binding",
                            status=401,
                            category="auth",
                        )
                    if await self.store.requires_fresh_presence(
                        session,
                        owner_domain_id=str(requested),
                        hardware_identity_ref=identity_ref,
                    ):
                        raise AdmissionProblem(
                            "FORBIDDEN",
                            "this Body was rejected or removed and needs a new commissioning",
                            status=403,
                            category="forbidden",
                        )
                self.store.add_proposal(
                    session,
                    enrollment_id=enrollment_id,
                    device_instance_id=payload["device_instance_candidate_id"],
                    hardware_identity_ref=identity_ref,
                    requested_owner_domain_id=str(requested),
                    state="pending_review",
                    revision=1,
                    hardware_evidence_digest=hardware["evidence_digest"],
                    commissioning_proof_digest="sha256:"
                    + hashlib.sha256(proof["proof"].encode()).hexdigest(),
                    manifest_id=manifest_ref.manifest_id,
                    manifest_revision=manifest_ref.revision,
                    manifest_digest=manifest_ref.digest,
                    manifest_json=canonical_manifest.decode("utf-8"),
                    handoff_public_key_spki=payload["handoff_key"]["public_key"],
                    handoff_key_id=handoff_key_id,
                    operational_public_key_spki=payload["operational_key"]["public_key"],
                    operational_key_id=operational_key_id,
                    collection_challenge_hash=hashlib.sha256(challenge.encode()).hexdigest(),
                    created_at=now,
                    expires_at=now + self.proposal_ttl,
                    updated_at=now,
                )
                await self.store.save_result(
                    session,
                    owner_domain_id=str(requested),
                    command_type="admission.create-enrollment",
                    command_id=command_id,
                    fingerprint=fp,
                    result=result,
                    occurred_at=now,
                )
                self.store.add_outbox(
                    session,
                    event_id=event_id,
                    event_type=event["type"],
                    aggregate_id=enrollment_id,
                    aggregate_revision=1,
                    event=event,
                    occurred_at=now,
                )
        return result

    async def expire_due(self) -> int:
        """Persist deadline terminal facts; callers never infer expiry from a read."""

        now = self.clock.now()
        expirable = (
            "pending_review",
            "approved_awaiting_handoff",
            "grant_delivered",
        )
        async with self.store.lock:
            async with self.store.transaction() as session:
                proposals = await self.store.list_expirable(session, states=expirable, deadline=now)
                for proposal in proposals:
                    require_transition(proposal.state, "expired")
                    proposal.state = "expired"
                    proposal.revision += 1
                    proposal.updated_at = now
                    grant = await self.store.get_grant_for_enrollment(
                        session, proposal.enrollment_id
                    )
                    if grant is not None and grant.revoked_at is None:
                        grant.revoked_at = now
                    event_id = self.ids.new("admission-event")
                    event_type = "live.eidolon.device.enrollment-expired.v1"
                    event = self._event(
                        event_id=event_id,
                        event_type=event_type,
                        subject=f"enrollments/{proposal.enrollment_id}",
                        owner_domain_id=proposal.requested_owner_domain_id,
                        revision=proposal.revision,
                        command_id=f"deadline:{proposal.enrollment_id}",
                        correlation_id=proposal.enrollment_id,
                        occurred_at=now,
                        data={"enrollment_id": proposal.enrollment_id},
                    )
                    self.store.add_outbox(
                        session,
                        event_id=event_id,
                        event_type=event_type,
                        aggregate_id=proposal.enrollment_id,
                        aggregate_revision=proposal.revision,
                        event=event,
                        occurred_at=now,
                    )
                return len(proposals)

    async def cancel_enrollment(
        self,
        *,
        command_id: str,
        correlation_id: str,
        enrollment_id: str,
        reason: str,
        context: ActorContext,
    ) -> dict:
        context.require_scope("device.claim.approve")
        payload = {
            "enrollment_id": enrollment_id,
            "reason": reason,
            "actor": context.actor.model_dump(mode="json"),
        }
        fp = fingerprint("admission.cancel-enrollment", context.owner_domain_id, payload)
        replay = await self.store.replay(
            owner_domain_id=str(context.owner_domain_id),
            command_type="admission.cancel-enrollment",
            command_id=command_id,
            fingerprint=fp,
        )
        if replay is not None:
            return replay
        await self.expire_due()
        now = self.clock.now()
        async with self.store.lock:
            async with self.store.transaction() as session:
                locked_replay = await self.store.replay_in_session(
                    session,
                    owner_domain_id=str(context.owner_domain_id),
                    command_type="admission.cancel-enrollment",
                    command_id=command_id,
                    fingerprint=fp,
                )
                if locked_replay is not None:
                    return locked_replay
                proposal = await self.store.get_proposal(session, enrollment_id)
                if proposal is None or proposal.requested_owner_domain_id != str(
                    context.owner_domain_id
                ):
                    raise AdmissionProblem(
                        "NOT_FOUND", "enrollment not found", status=404, category="missing"
                    )
                require_transition(proposal.state, "canceled")
                proposal.state = "canceled"
                proposal.revision += 1
                proposal.updated_at = now
                result = {
                    "command_id": command_id,
                    "outcome": "committed",
                    "enrollment_id": enrollment_id,
                    "proposal_state": "canceled",
                    "proposal_revision": proposal.revision,
                    "occurred_at": now.isoformat().replace("+00:00", "Z"),
                }
                await self.store.save_result(
                    session,
                    owner_domain_id=str(context.owner_domain_id),
                    command_type="admission.cancel-enrollment",
                    command_id=command_id,
                    fingerprint=fp,
                    result=result,
                    occurred_at=now,
                )
                event_id = self.ids.new("admission-event")
                event_type = "live.eidolon.device.enrollment-canceled.v1"
                event = self._event(
                    event_id=event_id,
                    event_type=event_type,
                    subject=f"enrollments/{enrollment_id}",
                    owner_domain_id=str(context.owner_domain_id),
                    revision=proposal.revision,
                    command_id=command_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    data={"enrollment_id": enrollment_id, "reason": reason},
                )
                self.store.add_outbox(
                    session,
                    event_id=event_id,
                    event_type=event_type,
                    aggregate_id=enrollment_id,
                    aggregate_revision=proposal.revision,
                    event=event,
                    occurred_at=now,
                )
        return result

    async def decide_enrollment(
        self,
        *,
        command_id: str,
        correlation_id: str,
        enrollment_id: str,
        payload: dict,
        context: ActorContext,
    ) -> dict:
        context.require_scope("device.claim.approve")
        target_domain = OwnerDomainId(payload["target_owner_domain_id"])
        target_owner = BusinessOwnerId(payload["target_business_owner_id"])
        if target_domain != context.owner_domain_id:
            raise AdmissionProblem(
                "OWNER_DOMAIN_MISMATCH",
                "Decision target Owner Domain does not match authorization",
                status=403,
                category="forbidden",
            )
        if target_owner != context.business_owner_id:
            raise AdmissionProblem(
                "BUSINESS_OWNER_MISMATCH",
                "Decision business Owner does not match authorization",
                status=403,
                category="forbidden",
            )
        semantic = {
            **payload,
            "enrollment_id": enrollment_id,
            "actor": context.actor.model_dump(mode="json"),
        }
        fp = fingerprint("admission.decide-enrollment", target_domain, semantic)
        replay = await self.store.replay(
            owner_domain_id=str(target_domain),
            command_type="admission.decide-enrollment",
            command_id=command_id,
            fingerprint=fp,
        )
        if replay is not None:
            return replay
        await self.expire_due()
        now = self.clock.now()
        async with self.store.lock:
            async with self.store.transaction() as session:
                locked_replay = await self.store.replay_in_session(
                    session,
                    owner_domain_id=str(target_domain),
                    command_type="admission.decide-enrollment",
                    command_id=command_id,
                    fingerprint=fp,
                )
                if locked_replay is not None:
                    return locked_replay
                proposal = await self.store.get_proposal(session, enrollment_id)
                if proposal is None or proposal.requested_owner_domain_id != str(
                    context.owner_domain_id
                ):
                    raise AdmissionProblem(
                        "NOT_FOUND", "enrollment not found", status=404, category="missing"
                    )
                if proposal.state == "expired" or aware(proposal.expires_at) <= now:
                    raise AdmissionProblem(
                        "PROPOSAL_EXPIRED", "proposal expired", status=410, category="expired"
                    )
                require_transition(
                    proposal.state,
                    "approved_awaiting_handoff" if payload["decision"] == "approve" else "rejected",
                )
                if proposal.revision != payload["expected_proposal_revision"]:
                    raise AdmissionProblem("REVISION_CONFLICT", "proposal revision is stale")
                reviewed = ManifestRef.model_validate(payload["reviewed_manifest_ref"])
                if (reviewed.manifest_id, reviewed.revision, reviewed.digest) != (
                    proposal.manifest_id,
                    proposal.manifest_revision,
                    proposal.manifest_digest,
                ):
                    raise AdmissionProblem(
                        "REVISION_CONFLICT", "reviewed ManifestRef does not match Proposal"
                    )
                decision_id = self.ids.new("decision")
                decision = ApprovalDecision(
                    decision_id=decision_id,
                    enrollment_id=enrollment_id,
                    decision=payload["decision"],
                    actor=context.actor,
                    target_owner_domain_id=target_domain,
                    target_business_owner_id=target_owner,
                    reviewed_manifest_ref=reviewed,
                    expected_proposal_revision=proposal.revision,
                    decided_at=now,
                )
                self.store.add_decision(
                    session,
                    decision_id=decision_id,
                    enrollment_id=enrollment_id,
                    decision=decision.decision,
                    actor_json=json.dumps(decision.actor.model_dump(mode="json"), sort_keys=True),
                    target_owner_domain_id=str(target_domain),
                    target_business_owner_id=str(target_owner),
                    reviewed_manifest_json=json.dumps(
                        reviewed.model_dump(mode="json"), sort_keys=True
                    ),
                    expected_proposal_revision=proposal.revision,
                    decided_at=now,
                )
                proposal.state = (
                    "approved_awaiting_handoff" if decision.decision == "approve" else "rejected"
                )
                proposal.revision += 1
                proposal.updated_at = now
                grant_id = None
                if decision.decision == "approve":
                    current_generation = await self.store.max_claim_generation(
                        session,
                        owner_domain_id=str(target_domain),
                        hardware_identity_ref=proposal.hardware_identity_ref,
                    )
                    device_ref = DeviceRef(
                        device_instance_id=proposal.device_instance_id,
                        owner_domain_id=target_domain,
                        owner_domain_generation=self.owner_domain_generation,
                        claim_generation=current_generation + 1,
                        trust_epoch=1,
                    )
                    grant_id = self.ids.new("grant")
                    grant = ClaimGrant(
                        grant_id=grant_id,
                        enrollment_id=enrollment_id,
                        device_ref=device_ref,
                        manifest_ref=reviewed,
                        approval_decision_id=decision_id,
                        handoff_key_id=proposal.handoff_key_id,
                        operational_key_id=proposal.operational_key_id,
                        issued_at=now,
                        expires_at=now + self.grant_ttl,
                    )
                    self.store.add_grant(
                        session,
                        grant_id=grant_id,
                        enrollment_id=enrollment_id,
                        decision_id=decision_id,
                        owner_domain_id=str(target_domain),
                        hardware_identity_ref=proposal.hardware_identity_ref,
                        claim_generation=device_ref.claim_generation,
                        device_ref_json=json.dumps(
                            device_ref.model_dump(mode="json"), sort_keys=True
                        ),
                        manifest_ref_json=json.dumps(
                            reviewed.model_dump(mode="json"), sort_keys=True
                        ),
                        handoff_key_id=proposal.handoff_key_id,
                        operational_key_id=proposal.operational_key_id,
                        grant_json=json.dumps(grant.model_dump(mode="json"), sort_keys=True),
                        wire_envelope_json=None,
                        issued_at=now,
                        expires_at=now + self.grant_ttl,
                        delivered_at=None,
                        revoked_at=None,
                    )
                result = {
                    "command_id": command_id,
                    "outcome": "committed",
                    "decision": decision.model_dump(mode="json"),
                    "grant_id": grant_id,
                    "proposal_revision": decision.expected_proposal_revision,
                    "occurred_at": now.isoformat().replace("+00:00", "Z"),
                }
                await self.store.save_result(
                    session,
                    owner_domain_id=str(target_domain),
                    command_type="admission.decide-enrollment",
                    command_id=command_id,
                    fingerprint=fp,
                    result=result,
                    occurred_at=now,
                )
                event_id = self.ids.new("admission-event")
                event_type = (
                    "live.eidolon.device.enrollment-approved.v1"
                    if decision.decision == "approve"
                    else "live.eidolon.device.enrollment-rejected.v1"
                )
                event = self._event(
                    event_id=event_id,
                    event_type=event_type,
                    subject=f"enrollments/{enrollment_id}",
                    owner_domain_id=str(target_domain),
                    revision=proposal.revision,
                    command_id=command_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    data={"decision_id": decision_id, "decision": decision.decision},
                )
                self.store.add_outbox(
                    session,
                    event_id=event_id,
                    event_type=event_type,
                    aggregate_id=enrollment_id,
                    aggregate_revision=proposal.revision,
                    event=event,
                    occurred_at=now,
                )
        return result

    async def collect_claim_grant(
        self,
        *,
        command_id: str,
        correlation_id: str,
        enrollment_id: str,
        proposal_revision: int,
        collection_challenge: str,
        handoff_key_proof: str,
    ) -> dict:
        fp_payload = {
            "enrollment_id": enrollment_id,
            "proposal_revision": proposal_revision,
            "collection_challenge_hash": hashlib.sha256(collection_challenge.encode()).hexdigest(),
            "handoff_key_proof": handoff_key_proof,
        }
        fp = fingerprint("admission.collect-claim-grant", self.owner_domain_id, fp_payload)
        replay = await self.store.replay(
            owner_domain_id=str(self.owner_domain_id),
            command_type="admission.collect-claim-grant",
            command_id=command_id,
            fingerprint=fp,
        )
        if replay is not None:
            return replay
        await self.expire_due()
        now = self.clock.now()
        async with self.store.lock:
            async with self.store.transaction() as session:
                locked_replay = await self.store.replay_in_session(
                    session,
                    owner_domain_id=str(self.owner_domain_id),
                    command_type="admission.collect-claim-grant",
                    command_id=command_id,
                    fingerprint=fp,
                )
                if locked_replay is not None:
                    return locked_replay
                proposal = await self.store.get_proposal(session, enrollment_id)
                if proposal is None or proposal.requested_owner_domain_id != str(
                    self.owner_domain_id
                ):
                    raise AdmissionProblem(
                        "NOT_FOUND", "enrollment not found", status=404, category="missing"
                    )
                if proposal.state == "claim_revoked":
                    raise AdmissionProblem("CLAIM_REVOKED", "Claim generation was revoked")
                if proposal.state == "grant_acknowledged":
                    raise AdmissionProblem(
                        "PROPOSAL_TERMINAL",
                        "Claim is active and the handoff capability is terminal",
                    )
                if proposal.state == "expired" or aware(proposal.expires_at) <= now:
                    raise AdmissionProblem(
                        "PROPOSAL_EXPIRED", "proposal expired", status=410, category="expired"
                    )
                grant = await self.store.get_grant_for_enrollment(session, enrollment_id)
                if grant is not None and grant.revoked_at is not None:
                    raise AdmissionProblem("CLAIM_REVOKED", "Claim generation was revoked")
                if (
                    grant is not None
                    and proposal.state == "grant_delivered"
                    and grant.wire_envelope_json is None
                ):
                    raise AdmissionProblem(
                        "CONTRACT_UNSUPPORTED",
                        "pre-PH2-B0 opaque grant cannot be replayed as a canonical wire envelope",
                    )
                was_delivered = grant is not None and grant.wire_envelope_json is not None
                decision = await self.store.get_decision_for_enrollment(session, enrollment_id)
                if decision is None or proposal.state not in {
                    "approved_awaiting_handoff",
                    "grant_delivered",
                }:
                    # Asked before this file first, because "nobody has decided
                    # yet" is the normal state of a device waiting to be added,
                    # and it used to be answered REVISION_CONFLICT: an
                    # undecided Proposal has no expected revision, so the
                    # comparison below could only fail. A device reading its own
                    # normal wait as a stale-revision conflict is a device that
                    # stops waiting.
                    raise AdmissionProblem("DECISION_REQUIRED", "approved Decision is required")
                if decision.expected_proposal_revision != proposal_revision:
                    raise AdmissionProblem("REVISION_CONFLICT", "proposal revision is stale")
                if (
                    proposal.collection_challenge_hash
                    != hashlib.sha256(collection_challenge.encode()).hexdigest()
                ):
                    raise AdmissionProblem(
                        "HANDOFF_PROOF_INVALID",
                        "collection challenge is invalid",
                        status=403,
                        category="forbidden",
                    )
                proof_doc = {
                    "contract": "eidolon.device-foundation.claim-grant-collection",
                    "enrollment_id": enrollment_id,
                    "proposal_revision": proposal_revision,
                    "collection_challenge": collection_challenge,
                }
                if not verify_p256_proof(
                    proposal.handoff_public_key_spki, proof_doc, handoff_key_proof
                ):
                    raise AdmissionProblem(
                        "HANDOFF_PROOF_INVALID",
                        "handoff key proof is invalid",
                        status=403,
                        category="forbidden",
                    )
                if grant is None:
                    raise AdmissionProblem("DECISION_REQUIRED", "approved ClaimGrant is missing")
                if aware(grant.expires_at) <= now:
                    raise AdmissionProblem(
                        "GRANT_EXPIRED", "ClaimGrant expired", status=410, category="expired"
                    )
                if not was_delivered:
                    grant_doc = json.loads(grant.grant_json)
                    aad = ClaimGrantAAD(
                        enrollment_id=enrollment_id,
                        proposal_revision=proposal_revision,
                        device_instance_id=proposal.device_instance_id,
                        hardware_evidence_digest=proposal.hardware_evidence_digest,
                        manifest_ref=ManifestRef(
                            manifest_id=proposal.manifest_id,
                            revision=proposal.manifest_revision,
                            digest=proposal.manifest_digest,
                        ),
                        owner_domain_id=proposal.requested_owner_domain_id,
                        owner_domain_generation=self.owner_domain_generation,
                        claim_generation=grant.claim_generation,
                        trust_epoch=1,
                        grant_id=grant.grant_id,
                    )
                    envelope = seal_claim_grant(
                        proposal.handoff_public_key_spki,
                        grant_doc,
                        aad,
                        recipient_handoff_key_id=proposal.handoff_key_id,
                    )
                    grant.wire_envelope_json = envelope.model_dump_json()
                    grant.delivered_at = now
                    proposal.state = "grant_delivered"
                    proposal.revision += 1
                    proposal.updated_at = now
                result = {
                    "command_id": command_id,
                    "outcome": "committed",
                    "grant_id": grant.grant_id,
                    "wire_envelope": json.loads(grant.wire_envelope_json),
                    "expires_at": aware(grant.expires_at).isoformat().replace("+00:00", "Z"),
                    "approval_decision_id": grant.decision_id,
                    "proposal_revision": proposal_revision,
                    "occurred_at": aware(grant.delivered_at).isoformat().replace("+00:00", "Z"),
                }
                await self.store.save_result(
                    session,
                    owner_domain_id=str(self.owner_domain_id),
                    command_type="admission.collect-claim-grant",
                    command_id=command_id,
                    fingerprint=fp,
                    result=result,
                    occurred_at=aware(grant.delivered_at),
                )
                if not was_delivered:
                    event_id = self.ids.new("admission-event")
                    event_type = "live.eidolon.device.claim-grant-delivered.v1"
                    event = self._event(
                        event_id=event_id,
                        event_type=event_type,
                        subject=f"enrollments/{enrollment_id}",
                        owner_domain_id=str(self.owner_domain_id),
                        revision=proposal.revision,
                        command_id=command_id,
                        correlation_id=correlation_id,
                        occurred_at=aware(grant.delivered_at),
                        data={
                            "grant_id": grant.grant_id,
                            "approval_decision_id": grant.decision_id,
                        },
                    )
                    self.store.add_outbox(
                        session,
                        event_id=event_id,
                        event_type=event_type,
                        aggregate_id=enrollment_id,
                        aggregate_revision=proposal.revision,
                        event=event,
                        occurred_at=aware(grant.delivered_at),
                    )
        return result

    async def ack_claim_grant(
        self,
        *,
        command_id: str,
        correlation_id: str,
        enrollment_id: str,
        grant_id: str,
        operational_key_proof: str,
        stored_claim_generation: int,
        stored_trust_epoch: int,
    ) -> dict:
        payload = {
            "enrollment_id": enrollment_id,
            "grant_id": grant_id,
            "operational_key_proof": operational_key_proof,
            "stored_claim_generation": stored_claim_generation,
            "stored_trust_epoch": stored_trust_epoch,
        }
        fp = fingerprint("admission.ack-claim-grant", self.owner_domain_id, payload)
        replay = await self.store.replay(
            owner_domain_id=str(self.owner_domain_id),
            command_type="admission.ack-claim-grant",
            command_id=command_id,
            fingerprint=fp,
        )
        if replay is not None:
            await self._project_committed_claim(replay)
            return replay
        await self.expire_due()
        now = self.clock.now()
        async with self.store.lock:
            async with self.store.transaction() as session:
                locked_replay = await self.store.replay_in_session(
                    session,
                    owner_domain_id=str(self.owner_domain_id),
                    command_type="admission.ack-claim-grant",
                    command_id=command_id,
                    fingerprint=fp,
                )
                if locked_replay is not None:
                    await self._project_committed_claim(locked_replay)
                    return locked_replay
                proposal = await self.store.get_proposal(session, enrollment_id)
                grant = await self.store.get_grant(session, grant_id)
                if proposal is None or grant is None or grant.enrollment_id != enrollment_id:
                    raise AdmissionProblem(
                        "NOT_FOUND", "ClaimGrant not found", status=404, category="missing"
                    )
                if grant.revoked_at is not None or proposal.state == "claim_revoked":
                    raise AdmissionProblem("CLAIM_REVOKED", "Claim generation was revoked")
                if aware(grant.expires_at) <= now:
                    raise AdmissionProblem(
                        "GRANT_EXPIRED", "ClaimGrant expired", status=410, category="expired"
                    )
                require_transition(proposal.state, "grant_acknowledged")
                device_ref = DeviceRef.model_validate(json.loads(grant.device_ref_json))
                if (stored_claim_generation, stored_trust_epoch) != (
                    device_ref.claim_generation,
                    device_ref.trust_epoch,
                ):
                    raise AdmissionProblem(
                        "GENERATION_CONFLICT", "stored Claim generation or trust epoch is stale"
                    )
                proof_doc = {
                    "contract": "eidolon.device-foundation.claim-grant-ack",
                    "enrollment_id": enrollment_id,
                    "grant_id": grant_id,
                    "device_ref": device_ref.model_dump(mode="json"),
                }
                if not verify_p256_proof(
                    proposal.operational_public_key_spki, proof_doc, operational_key_proof
                ):
                    raise AdmissionProblem(
                        "UNAUTHENTICATED",
                        "operational key proof is invalid",
                        status=401,
                        category="auth",
                    )
                proof_fp = "sha256:" + hashlib.sha256(operational_key_proof.encode()).hexdigest()
                self.store.add_grant_ack(
                    session,
                    grant_id=grant_id,
                    enrollment_id=enrollment_id,
                    proof_fingerprint=proof_fp,
                    device_ref_json=grant.device_ref_json,
                    acknowledged_at=now,
                )
                decision = await self.store.get_decision(session, grant.decision_id)
                manifest_ref = ManifestRef.model_validate(json.loads(grant.manifest_ref_json))
                await self.store.upsert_claim(
                    session,
                    device_instance_id=device_ref.device_instance_id,
                    owner_domain_id=str(device_ref.owner_domain_id),
                    business_owner_id=decision.target_business_owner_id,
                    hardware_identity_ref=grant.hardware_identity_ref,
                    owner_domain_generation=device_ref.owner_domain_generation,
                    claim_generation=device_ref.claim_generation,
                    trust_epoch=device_ref.trust_epoch,
                    manifest_ref_json=grant.manifest_ref_json,
                    approval_decision_id=grant.decision_id,
                    operational_public_key_spki=proposal.operational_public_key_spki,
                    state="active",
                    revision=1,
                    activated_at=now,
                    updated_at=now,
                    revoked_at=None,
                )
                await self.store.project_active_claim(
                    session,
                    device_ref=device_ref,
                    business_owner_id=decision.target_business_owner_id,
                    manifest_id=proposal.manifest_id,
                    manifest_json=proposal.manifest_json,
                    manifest_digest=proposal.manifest_digest,
                    manifest_declared_revision=proposal.manifest_revision,
                    activated_at=now,
                )
                proposal.state = "grant_acknowledged"
                proposal.revision += 1
                proposal.updated_at = now
                result = {
                    "command_id": command_id,
                    "outcome": "committed",
                    "device_ref": device_ref.model_dump(mode="json"),
                    "claim_state": "active",
                    "occurred_at": now.isoformat().replace("+00:00", "Z"),
                }
                await self.store.save_result(
                    session,
                    owner_domain_id=str(self.owner_domain_id),
                    command_type="admission.ack-claim-grant",
                    command_id=command_id,
                    fingerprint=fp,
                    result=result,
                    occurred_at=now,
                )
                ack_event_id = self.ids.new("admission-event")
                ack_event_type = "live.eidolon.device.claim-grant-acknowledged.v1"
                ack_event = self._event(
                    event_id=ack_event_id,
                    event_type=ack_event_type,
                    subject=f"enrollments/{enrollment_id}",
                    owner_domain_id=str(device_ref.owner_domain_id),
                    revision=proposal.revision,
                    command_id=command_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    data={"grant_id": grant_id},
                )
                self.store.add_outbox(
                    session,
                    event_id=ack_event_id,
                    event_type=ack_event_type,
                    aggregate_id=enrollment_id,
                    aggregate_revision=proposal.revision,
                    event=ack_event,
                    occurred_at=now,
                )
                event_id = self.ids.new("admission-event")
                event_type = "live.eidolon.device.claim-activated.v1"
                event = self._event(
                    event_id=event_id,
                    event_type=event_type,
                    subject=f"device-instances/{device_ref.device_instance_id}",
                    owner_domain_id=str(device_ref.owner_domain_id),
                    revision=1,
                    command_id=command_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    data={
                        "device_ref": device_ref.model_dump(mode="json"),
                        "business_owner_id": decision.target_business_owner_id,
                        "manifest_ref": manifest_ref.model_dump(mode="json"),
                        "approval_decision_id": grant.decision_id,
                        "activated_at": now.isoformat().replace("+00:00", "Z"),
                    },
                )
                event = ClaimActivatedEvent.model_validate(event).model_dump(mode="json")
                self.store.add_outbox(
                    session,
                    event_id=event_id,
                    event_type=event_type,
                    aggregate_id=device_ref.device_instance_id,
                    aggregate_revision=1,
                    event=event,
                    occurred_at=now,
                )
        await self._project_committed_claim(result)
        return result

    async def revoke_claim(
        self,
        *,
        command_id: str,
        correlation_id: str,
        device_ref: DeviceRef,
        reason: str,
        context: ActorContext,
    ) -> dict:
        context.require_scope("device.claim.revoke")
        if device_ref.owner_domain_id != context.owner_domain_id:
            raise AdmissionProblem(
                "OWNER_DOMAIN_MISMATCH",
                "target DeviceRef is outside authorization",
                status=403,
                category="forbidden",
            )
        payload = {"device_ref": device_ref.model_dump(mode="json"), "reason": reason}
        fp = fingerprint("admission.revoke-claim", context.owner_domain_id, payload)
        replay = await self.store.replay(
            owner_domain_id=str(context.owner_domain_id),
            command_type="admission.revoke-claim",
            command_id=command_id,
            fingerprint=fp,
        )
        if replay is not None:
            return replay
        now = self.clock.now()
        async with self.store.lock:
            async with self.store.transaction() as session:
                locked_replay = await self.store.replay_in_session(
                    session,
                    owner_domain_id=str(context.owner_domain_id),
                    command_type="admission.revoke-claim",
                    command_id=command_id,
                    fingerprint=fp,
                )
                if locked_replay is not None:
                    return locked_replay
                claim = await self.store.get_claim(session, device_ref.device_instance_id)
                grant = await self.store.get_grant_for_device_ref(
                    session,
                    json.dumps(device_ref.model_dump(mode="json"), sort_keys=True),
                )
                if claim is None and grant is None:
                    raise AdmissionProblem(
                        "NOT_FOUND", "Claim not found", status=404, category="missing"
                    )
                business_owner = (
                    claim.business_owner_id
                    if claim
                    else (
                        await self.store.get_decision(session, grant.decision_id)
                    ).target_business_owner_id
                )
                if business_owner != str(context.business_owner_id):
                    raise AdmissionProblem(
                        "NOT_FOUND", "Claim not found", status=404, category="missing"
                    )
                if claim is not None and (
                    claim.owner_domain_id,
                    claim.owner_domain_generation,
                    claim.claim_generation,
                    claim.trust_epoch,
                ) != (
                    str(device_ref.owner_domain_id),
                    device_ref.owner_domain_generation,
                    device_ref.claim_generation,
                    device_ref.trust_epoch,
                ):
                    raise AdmissionProblem("GENERATION_CONFLICT", "Claim generation is stale")
                if claim is None:
                    proposal = await self.store.get_proposal(session, grant.enrollment_id)
                    require_transition(proposal.state, "claim_revoked")
                    proposal.state = "claim_revoked"
                    proposal.revision += 1
                    proposal.updated_at = now
                    grant.revoked_at = now
                    decision = await self.store.get_decision(session, grant.decision_id)
                    await self.store.upsert_claim(
                        session,
                        device_instance_id=device_ref.device_instance_id,
                        owner_domain_id=str(device_ref.owner_domain_id),
                        business_owner_id=decision.target_business_owner_id,
                        hardware_identity_ref=grant.hardware_identity_ref,
                        owner_domain_generation=device_ref.owner_domain_generation,
                        claim_generation=device_ref.claim_generation,
                        trust_epoch=device_ref.trust_epoch,
                        manifest_ref_json=grant.manifest_ref_json,
                        approval_decision_id=grant.decision_id,
                        operational_public_key_spki=proposal.operational_public_key_spki,
                        state="revoked",
                        revision=1,
                        activated_at=None,
                        updated_at=now,
                        revoked_at=now,
                    )
                    revision = 1
                else:
                    if claim.state == "revoked":
                        occurred_at = aware(claim.revoked_at or claim.updated_at)
                        result = {
                            "operation": "device.claim-revocation-result",
                            "command_id": command_id,
                            "outcome": "committed",
                            "device_ref": device_ref.model_dump(mode="json"),
                            "aggregate_revision": claim.revision,
                            "event_id": None,
                            "lifecycle_state": "revoked",
                            "claim_state": "revoked",
                            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
                        }
                        await self.store.save_result(
                            session,
                            owner_domain_id=str(context.owner_domain_id),
                            command_type="admission.revoke-claim",
                            command_id=command_id,
                            fingerprint=fp,
                            result=result,
                            occurred_at=occurred_at,
                        )
                        return result
                    else:
                        claim.state = "revoked"
                        claim.revision += 1
                        claim.updated_at = now
                        claim.revoked_at = now
                        active_grant = await self.store.get_grant_for_decision(
                            session, claim.approval_decision_id
                        )
                        if active_grant is not None:
                            active_grant.revoked_at = now
                        revision = claim.revision
                await self.store.project_revoked_claim(
                    session, device_ref=device_ref, revoked_at=now
                )
                event_id = self.ids.new("admission-event")
                result = {
                    "operation": "device.claim-revocation-result",
                    "command_id": command_id,
                    "outcome": "committed",
                    "device_ref": device_ref.model_dump(mode="json"),
                    "aggregate_revision": revision,
                    "event_id": event_id,
                    "lifecycle_state": "revoked",
                    "claim_state": "revoked",
                    "occurred_at": now.isoformat().replace("+00:00", "Z"),
                }
                await self.store.save_result(
                    session,
                    owner_domain_id=str(context.owner_domain_id),
                    command_type="admission.revoke-claim",
                    command_id=command_id,
                    fingerprint=fp,
                    result=result,
                    occurred_at=now,
                )
                event_type = "live.eidolon.device.claim-revoked.v1"
                event = self._event(
                    event_id=event_id,
                    event_type=event_type,
                    subject=f"device-instances/{device_ref.device_instance_id}",
                    owner_domain_id=str(device_ref.owner_domain_id),
                    revision=revision,
                    command_id=command_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    data={
                        "device_ref": device_ref.model_dump(mode="json"),
                        "reason": reason,
                        "revoked_at": now.isoformat().replace("+00:00", "Z"),
                    },
                )
                event = ClaimRevokedEvent.model_validate(event).model_dump(mode="json")
                self.store.add_outbox(
                    session,
                    event_id=event_id,
                    event_type=event_type,
                    aggregate_id=device_ref.device_instance_id,
                    aggregate_revision=revision,
                    event=event,
                    occurred_at=now,
                )
        return result

    async def base_identity_for_key(
        self, *, operational_key_id: str, context: ActorContext
    ) -> dict:
        """Which base identity this Owner Domain issued to this operational key.

        The Host asks before it signs a voucher, because only the Hub knows.
        It asks by key and not by the identity a device claims: the key is the
        one thing a device can prove, so nothing it says about its own name has
        to be believed or checked — it is never asked. A Body that was removed
        comes back on the identity that answer names, which is what keeps its
        Claim generation fence attached to it.
        """

        context.require_scope("device.read")
        async with self.store.transaction() as session:
            row = await self.store.base_identity_for_key(session, operational_key_id)
            requires_presence = (
                await self.store.requires_fresh_presence(
                    session,
                    owner_domain_id=str(context.owner_domain_id),
                    hardware_identity_ref=row.hardware_identity_ref,
                )
                if row is not None
                else False
            )
        known = row is not None and row.owner_domain_id == str(context.owner_domain_id)
        return {
            "contract_version": "1",
            "operational_key_id": operational_key_id,
            "device_base_id": row.device_base_id if known else None,
            "requires_fresh_presence": requires_presence,
        }

    async def get_claim(self, *, device_instance_id: str, context: ActorContext) -> ClaimRecord:
        context.require_scope("device.read")
        row = await self.store.load_claim(device_instance_id=device_instance_id)
        if (
            row is None
            or row.owner_domain_id != str(context.owner_domain_id)
            or row.business_owner_id != str(context.business_owner_id)
        ):
            raise AdmissionProblem("NOT_FOUND", "Claim not found", status=404, category="missing")
        return ClaimRecord(
            device_ref=DeviceRef(
                device_instance_id=row.device_instance_id,
                owner_domain_id=row.owner_domain_id,
                owner_domain_generation=row.owner_domain_generation,
                claim_generation=row.claim_generation,
                trust_epoch=row.trust_epoch,
            ),
            business_owner_id=row.business_owner_id,
            manifest_ref=ManifestRef.model_validate(json.loads(row.manifest_ref_json)),
            state=ClaimState(row.state),
            revision=row.revision,
            updated_at=aware(row.updated_at),
        )

    @staticmethod
    def _proposal(row, *, content_revision: int) -> EnrollmentProposal:
        return EnrollmentProposal(
            enrollment_id=row.enrollment_id,
            proposal_revision=content_revision,
            state=EnrollmentProposalState(row.state),
            device_instance_candidate_id=row.device_instance_id,
            requested_owner_domain_id=row.requested_owner_domain_id,
            hardware_evidence_digest=row.hardware_evidence_digest,
            manifest_ref=ManifestRef(
                manifest_id=row.manifest_id,
                revision=row.manifest_revision,
                digest=row.manifest_digest,
            ),
            handoff_key_id=row.handoff_key_id,
            created_at=aware(row.created_at),
            expires_at=aware(row.expires_at),
        )

    @staticmethod
    def _claim(row) -> ClaimRecord:
        return ClaimRecord(
            device_ref=DeviceRef(
                device_instance_id=row.device_instance_id,
                owner_domain_id=row.owner_domain_id,
                owner_domain_generation=row.owner_domain_generation,
                claim_generation=row.claim_generation,
                trust_epoch=row.trust_epoch,
            ),
            business_owner_id=row.business_owner_id,
            manifest_ref=ManifestRef.model_validate(json.loads(row.manifest_ref_json)),
            state=ClaimState(row.state),
            revision=row.revision,
            updated_at=aware(row.updated_at),
        )

    async def _recovery_projection(self, session, proposal) -> EnrollmentRecoveryProjection:
        decision_row = await self.store.get_decision_for_enrollment(session, proposal.enrollment_id)
        grant_row = await self.store.get_grant_for_enrollment(session, proposal.enrollment_id)
        ack_row = await self.store.get_ack_for_enrollment(session, proposal.enrollment_id)
        claim_row = (
            await self.store.get_claim(session, proposal.device_instance_id)
            if grant_row is not None
            else None
        )
        decision = None
        if decision_row is not None:
            decision = ApprovalDecision(
                decision_id=decision_row.decision_id,
                enrollment_id=decision_row.enrollment_id,
                decision=decision_row.decision,
                actor=json.loads(decision_row.actor_json),
                target_owner_domain_id=decision_row.target_owner_domain_id,
                target_business_owner_id=decision_row.target_business_owner_id,
                reviewed_manifest_ref=json.loads(decision_row.reviewed_manifest_json),
                expected_proposal_revision=decision_row.expected_proposal_revision,
                decided_at=aware(decision_row.decided_at),
            )
        delivery = None
        if grant_row is not None and grant_row.delivered_at is not None:
            delivery = GrantDeliveryRecord(
                grant_id=grant_row.grant_id,
                approval_decision_id=grant_row.decision_id,
                state="acknowledged" if ack_row is not None else "delivered",
                delivered_at=aware(grant_row.delivered_at),
                acknowledged_at=(aware(ack_row.acknowledged_at) if ack_row is not None else None),
            )
        return EnrollmentRecoveryProjection(
            proposal=self._proposal(
                proposal,
                content_revision=(
                    decision_row.expected_proposal_revision if decision_row is not None else 1
                ),
            ),
            approval_decision=decision,
            grant_delivery=delivery,
            claim=self._claim(claim_row) if claim_row is not None else None,
            source_revision=proposal.revision,
            observed_at=self.clock.now(),
        )

    async def get_enrollment_recovery(
        self, *, enrollment_id: str, context: ActorContext
    ) -> EnrollmentRecoveryProjection:
        context.require_scope("device.read")
        await self.expire_due()
        async with self.store.database.sessions() as session:
            proposal = await self.store.get_proposal(session, enrollment_id)
            if proposal is None or proposal.requested_owner_domain_id != str(
                context.owner_domain_id
            ):
                raise AdmissionProblem(
                    "NOT_FOUND", "enrollment not found", status=404, category="missing"
                )
            decision = await self.store.get_decision_for_enrollment(session, enrollment_id)
            is_domain_approver = "device.claim.approve" in context.actor.granted_scopes
            if decision is None and not is_domain_approver:
                raise AdmissionProblem(
                    "NOT_FOUND", "enrollment not found", status=404, category="missing"
                )
            if (
                decision is not None
                and decision.target_business_owner_id != str(context.business_owner_id)
                and not is_domain_approver
            ):
                raise AdmissionProblem(
                    "NOT_FOUND", "enrollment not found", status=404, category="missing"
                )
            return await self._recovery_projection(session, proposal)

    async def list_enrollment_recovery(
        self, *, query: EnrollmentProposalQuery, context: ActorContext
    ) -> EnrollmentProposalPage:
        context.require_scope("device.claim.approve")
        if query.owner_domain_id != context.owner_domain_id:
            raise AdmissionProblem(
                "NOT_FOUND", "enrollments not found", status=404, category="missing"
            )
        await self.expire_due()
        cursor = (
            (query.cursor.sort_key, query.cursor.resource_id) if query.cursor is not None else None
        )
        async with self.store.database.sessions() as session:
            rows = await self.store.list_proposals(
                session,
                owner_domain_id=str(context.owner_domain_id),
                states=tuple(state.value for state in query.states),
                cursor=cursor,
                limit=query.limit,
            )
            items = tuple([await self._recovery_projection(session, row) for row in rows])
        next_cursor = None
        if len(rows) == query.limit:
            last = rows[-1]
            next_cursor = AdmissionListCursor(
                owner_domain_id=context.owner_domain_id,
                sort_key=aware(last.created_at),
                resource_id=last.enrollment_id,
            )
        return EnrollmentProposalPage(
            owner_domain_id=context.owner_domain_id,
            items=items,
            next_cursor=next_cursor,
            observed_at=self.clock.now(),
        )

    async def list_claims(self, *, query: ClaimQuery, context: ActorContext) -> ClaimPage:
        context.require_scope("device.read")
        if query.owner_domain_id != context.owner_domain_id:
            return ClaimPage(
                owner_domain_id=context.owner_domain_id,
                items=(),
                next_cursor=None,
                observed_at=self.clock.now(),
            )
        cursor = (
            (query.cursor.sort_key, query.cursor.resource_id) if query.cursor is not None else None
        )
        async with self.store.database.sessions() as session:
            rows = await self.store.list_claims(
                session,
                owner_domain_id=str(context.owner_domain_id),
                business_owner_id=str(context.business_owner_id),
                states=tuple(state.value for state in query.states),
                cursor=cursor,
                limit=query.limit,
            )
        next_cursor = None
        if len(rows) == query.limit:
            last = rows[-1]
            next_cursor = AdmissionListCursor(
                owner_domain_id=context.owner_domain_id,
                sort_key=aware(last.updated_at),
                resource_id=last.device_instance_id,
            )
        return ClaimPage(
            owner_domain_id=context.owner_domain_id,
            items=tuple(self._claim(row) for row in rows),
            next_cursor=next_cursor,
            observed_at=self.clock.now(),
        )

    async def claim_event_page(
        self, *, cursor: ClaimEventCursor, limit: int, owner_domain_id: OwnerDomainId
    ) -> ClaimEventPage:
        if owner_domain_id != self.owner_domain_id:
            raise AdmissionProblem(
                "NOT_FOUND", "Claim event stream not found", status=404, category="missing"
            )
        records, high_watermark = await self.store.claim_event_page(
            after=cursor.stream_position, limit=limit
        )
        items = tuple(
            ClaimEventStreamItem(stream_position=position, event=event)
            for position, event in records
        )
        next_position = items[-1].stream_position if items else cursor.stream_position
        return ClaimEventPage(
            requested_after=cursor,
            events=items,
            next_cursor=ClaimEventCursor(stream_position=next_position),
            high_watermark=high_watermark,
            observed_at=self.clock.now(),
        )
