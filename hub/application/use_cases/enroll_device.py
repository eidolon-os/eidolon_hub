"""Create a short-lived, human-approved device enrollment."""

from __future__ import annotations

from datetime import timedelta

from hub.application.idempotency import mutation_fingerprint
from hub.domain.devices.entities import (
    DeviceEnrollmentIntent,
    DeviceLifecycleState,
    ManagedDevice,
)
from hub.ports.identity import Clock, IdGenerator, RetrievalTokenHasher
from hub.ports.management_events import (
    DeviceManagementEventRecord,
)
from hub.ports.repositories import (
    DeviceDirectoryProjector,
    DeviceMutationUnitOfWork,
    DeviceRepository,
)


class EnrollDevice:
    def __init__(
        self,
        *,
        devices: DeviceRepository,
        mutations: DeviceMutationUnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        tokens: RetrievalTokenHasher,
        enrollment_ttl: timedelta,
        directory_projector: DeviceDirectoryProjector,
    ) -> None:
        self._devices = devices
        self._mutations = mutations
        self._clock = clock
        self._ids = ids
        self._tokens = tokens
        self._enrollment_ttl = enrollment_ttl
        self._directory_projector = directory_projector

    async def execute(self, enrollment: DeviceEnrollmentIntent) -> ManagedDevice:
        token_hash = self._tokens.hash(enrollment.retrieval_token)
        fingerprint = self._fingerprint(enrollment, token_hash=token_hash)
        current = await self._devices.get(enrollment.identity.device_id)
        now = self._clock.now()

        if current is not None and current.last_enrollment_request_id == enrollment.request_id:
            if current.last_enrollment_fingerprint != fingerprint:
                raise ValueError("enrollment request_id was reused with different content")
            await self._directory_projector.execute(current.identity.device_id)
            return current

        if current is not None:
            # Enrolling is asking for a grant, so it is refused exactly while a
            # grant exists: an approved device cannot be reset by anyone who
            # merely knows its id. A revocation ends the grant — the owner
            # removed this phone — and the device may ask again from scratch,
            # which is the only way one that was removed, or reinstalled, ever
            # comes back. An unapproved enrollment nobody collected in time is
            # likewise free to start over.
            can_restart = current.lifecycle_state is DeviceLifecycleState.REVOKED or (
                current.lifecycle_state is DeviceLifecycleState.PENDING_APPROVAL
                and current.retrieval_expires_at <= now
            )
            if not can_restart:
                raise ValueError("device is already enrolled")

        device = ManagedDevice(
            identity=enrollment.identity,
            enrollment_id=self._ids.new("enrollment"),
            retrieval_token_hash=token_hash,
            retrieval_expires_at=now + self._enrollment_ttl,
            display_name=enrollment.display_name,
            device_kind=enrollment.device_kind,
            manifest=enrollment.manifest,
            enrolled_at=now,
            updated_at=now,
            last_enrollment_request_id=enrollment.request_id,
            last_enrollment_fingerprint=fingerprint,
            claim_generation=(current.claim_generation + 1 if current is not None else 1),
            trust_epoch=(current.trust_epoch if current is not None else 1),
            aggregate_revision=(current.aggregate_revision + 1 if current is not None else 1),
        )
        persisted = await self._mutations.commit(
            expected=current,
            device=device,
            event=DeviceManagementEventRecord(
                event_id=enrollment.request_id,
                event_type="eidolon.device.enrolled.v1",
                source="eidolon-hub/device-management",
                principal_id=f"untrusted-device:{device.identity.device_id}",
                subject=device.identity.device_id,
                occurred_at=now,
                data={"manifest_revision": device.manifest_revision},
            ),
        )
        await self._directory_projector.execute(device.identity.device_id)
        return persisted

    @staticmethod
    def _fingerprint(enrollment: DeviceEnrollmentIntent, *, token_hash: str) -> str:
        return mutation_fingerprint(
            "device.enroll",
            {
                "device_id": enrollment.identity.device_id,
                "display_name": enrollment.display_name,
                "device_kind": enrollment.device_kind,
                "manifest_revision": enrollment.manifest.revision,
                "token_hash": token_hash,
            },
        )
