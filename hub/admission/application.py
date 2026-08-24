"""Canonical Proposal/Decision/Grant/Ack/Claim application service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Callable, Protocol

import rfc8785

from hub.admission.crypto import key_id, seal_claim_grant, verify_p256_proof
from hub.admission.domain import (
    ActorContext,
    AdmissionProblem,
    fingerprint,
    require_transition,
)
from hub.admission.persistence import SqlAdmissionStore, aware
from hub.contracts.bindings.admission import (
    ApprovalDecision,
    BusinessOwnerId,
    ClaimGrant,
    ClaimRecord,
    ClaimState,
    DeviceRef,
    ManifestRef,
    OwnerDomainId,
)
from hub.ports.identity import Clock, IdGenerator


class CommissioningProofVerifier(Protocol):
    def verify(
        self, *, device_instance_id: str, owner_domain_id: str, nonce: str, proof: str
    ) -> bool: ...


class HmacCommissioningProofVerifier:
    """Real test/development profile verifier backed by per-device setup secrets."""

    def __init__(self, secret_for_device: Callable[[str], bytes | None]) -> None:
        self._secret_for_device = secret_for_device

    def verify(
        self, *, device_instance_id: str, owner_domain_id: str, nonce: str, proof: str
    ) -> bool:
        secret = self._secret_for_device(device_instance_id)
        if secret is None:
            return False
        message = f"{device_instance_id}\0{owner_domain_id}\0{nonce}".encode()
        expected = (
            base64.urlsafe_b64encode(hmac.new(secret, message, hashlib.sha256).digest())
            .rstrip(b"=")
            .decode()
        )
        return hmac.compare_digest(expected, proof)


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
        proposal_ttl: timedelta = timedelta(minutes=15),
        grant_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.owner_domain_id = OwnerDomainId(owner_domain_id)
        self.owner_domain_generation = owner_domain_generation
        self.commissioning_proofs = commissioning_proofs
        self.proposal_ttl = proposal_ttl
        self.grant_ttl = grant_ttl

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
        return {
            "specversion": "1.0",
            "id": event_id,
            "source": AdmissionAuthority.SOURCE,
            "type": event_type,
            "subject": subject,
            "time": occurred_at.isoformat().replace("+00:00", "Z"),
            "datacontenttype": "application/json",
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
        proof = payload["commissioning_proof"]
        if not self.commissioning_proofs.verify(
            device_instance_id=payload["device_instance_candidate_id"],
            owner_domain_id=str(requested),
            nonce=proof["nonce"],
            proof=proof["proof"],
        ):
            raise AdmissionProblem(
                "UNAUTHENTICATED", "commissioning proof is invalid", status=401, category="auth"
            )
        try:
            handoff_key_id = key_id(payload["handoff_key"]["public_key"])
            operational_key_id = key_id(payload["operational_key"]["public_key"])
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
        now = self.clock.now()
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
                self.store.add_proposal(
                    session,
                        enrollment_id=enrollment_id,
                        device_instance_id=payload["device_instance_candidate_id"],
                        hardware_identity_ref=hardware.get(
                            "hardware_identity_ref", hardware["evidence_digest"]
                        ),
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
                proposals = await self.store.list_expirable(
                    session, states=expirable, deadline=now
                )
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
                        actor_json=json.dumps(
                            decision.actor.model_dump(mode="json"), sort_keys=True
                        ),
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
                            sealed_grant=None,
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
                    "proposal_revision": proposal.revision,
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
                if proposal.state == "expired" or aware(proposal.expires_at) <= now:
                    raise AdmissionProblem(
                        "PROPOSAL_EXPIRED", "proposal expired", status=410, category="expired"
                    )
                grant = await self.store.get_grant_for_enrollment(session, enrollment_id)
                if grant is not None and grant.revoked_at is not None:
                    raise AdmissionProblem("CLAIM_REVOKED", "Claim generation was revoked")
                was_delivered = grant is not None and grant.sealed_grant is not None
                expected_collection_revision = (
                    proposal.revision - 1 if was_delivered else proposal.revision
                )
                if expected_collection_revision != proposal_revision:
                    raise AdmissionProblem("REVISION_CONFLICT", "proposal revision is stale")
                if proposal.state not in {"approved_awaiting_handoff", "grant_delivered"}:
                    raise AdmissionProblem("DECISION_REQUIRED", "approved Decision is required")
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
                    aad = {
                        "contract": "eidolon.device-foundation.claim-grant-aad",
                        "profile_id": "eidolon-trust-p256-hpke-v1",
                        "enrollment_id": enrollment_id,
                        "proposal_revision": proposal_revision,
                        "hardware_evidence_digest": proposal.hardware_evidence_digest,
                        "manifest_digest": proposal.manifest_digest,
                        "owner_domain_id": proposal.requested_owner_domain_id,
                        "owner_domain_generation": self.owner_domain_generation,
                        "claim_generation": grant.claim_generation,
                        "trust_epoch": 1,
                        "grant_id": grant.grant_id,
                    }
                    grant.sealed_grant = seal_claim_grant(
                        proposal.handoff_public_key_spki, grant_doc, aad
                    )
                    grant.delivered_at = now
                    proposal.state = "grant_delivered"
                    proposal.revision += 1
                    proposal.updated_at = now
                result = {
                    "command_id": command_id,
                    "outcome": "committed",
                    "grant_id": grant.grant_id,
                    "sealed_grant": grant.sealed_grant,
                    "expires_at": aware(grant.expires_at).isoformat().replace("+00:00", "Z"),
                    "approval_decision_id": grant.decision_id,
                    "proposal_revision": proposal.revision,
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
                self.store.add_claim(
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
                        "manifest_ref": manifest_ref.model_dump(mode="json"),
                        "approval_decision_id": grant.decision_id,
                        "activated_at": now.isoformat().replace("+00:00", "Z"),
                    },
                )
                self.store.add_outbox(
                    session,
                    event_id=event_id,
                    event_type=event_type,
                    aggregate_id=device_ref.device_instance_id,
                    aggregate_revision=1,
                    event=event,
                    occurred_at=now,
                )
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
                    self.store.add_claim(
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
                            "command_id": command_id,
                            "outcome": "committed",
                            "device_ref": device_ref.model_dump(mode="json"),
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
                result = {
                    "command_id": command_id,
                    "outcome": "committed",
                    "device_ref": device_ref.model_dump(mode="json"),
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
                event_id = self.ids.new("admission-event")
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
