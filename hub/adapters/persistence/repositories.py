"""SQLAlchemy implementations of Hub persistence ports."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    ChallengeRow,
    ChannelCursorRow,
    ChannelLeaseRow,
    CommandRow,
    DeviceAuthorityRow,
    DeviceRow,
    DeviceSessionRow,
    DirectoryRow,
    EventRow,
)
from hub.domain.channels.entities import (
    ChannelKind,
    ChannelLease,
    ChannelState,
)
from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    DirectorySession,
    ManagedDevice,
)
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.domain.sessions.entities import (
    DeviceAuthorityLease,
    DeviceSessionLease,
    DeviceSessionState,
)
from hub.ports.event_bus import DomainEvent, StoredDomainEvent
from hub.ports.identity import EnrollmentChallenge


class AuthorityLeaseHeld(PermissionError):
    pass


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SqlDeviceRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, device_id: str) -> ManagedDevice | None:
        async with self._database.sessions() as session:
            row = await session.get(DeviceRow, device_id)
            return None if row is None else self._decode(row)

    async def upsert(self, device: ManagedDevice) -> ManagedDevice:
        async with self._database.sessions.begin() as session:
            row = await session.get(DeviceRow, device.identity.device_id)
            values = self._values(device)
            if row is None:
                row = DeviceRow(**values)
                session.add(row)
            else:
                for name, value in values.items():
                    setattr(row, name, value)
        return device

    async def list_all(self) -> tuple[ManagedDevice, ...]:
        async with self._database.sessions() as session:
            rows = (await session.scalars(select(DeviceRow).order_by(DeviceRow.device_id))).all()
            return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _values(device: ManagedDevice) -> dict[str, object]:
        return {
            "device_id": device.identity.device_id,
            "public_key_fingerprint": device.identity.public_key_fingerprint,
            "tenant_id": device.identity.tenant_id,
            "display_name": device.display_name,
            "device_kind": device.device_kind,
            "manifest_json": device.manifest_json,
            "manifest_revision": device.manifest_revision,
            "registered_at": device.registered_at,
            "updated_at": device.updated_at,
            "last_registration_request_id": device.last_registration_request_id,
            "owner_id": device.owner_id,
            "approved": device.approved,
            "revoked": device.revoked,
            "last_management_request_id": device.last_management_request_id,
            "last_management_fingerprint": device.last_management_fingerprint,
        }

    @staticmethod
    def _decode(row: DeviceRow) -> ManagedDevice:
        return ManagedDevice(
            identity=DeviceIdentity(
                row.device_id,
                row.public_key_fingerprint,
                row.tenant_id,
            ),
            display_name=row.display_name,
            device_kind=row.device_kind,
            manifest=DeviceManifestDocument(
                canonical_json=row.manifest_json,
                revision=row.manifest_revision,
            ),
            registered_at=_aware(row.registered_at),
            updated_at=_aware(row.updated_at),
            last_registration_request_id=row.last_registration_request_id,
            owner_id=row.owner_id,
            approved=row.approved,
            revoked=row.revoked,
            last_management_request_id=row.last_management_request_id,
            last_management_fingerprint=row.last_management_fingerprint,
        )


class SqlCommandRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, command_id: str) -> DeviceCommand | None:
        async with self._database.sessions() as session:
            row = await session.get(CommandRow, command_id)
            return None if row is None else self._decode(row)

    async def upsert(self, command: DeviceCommand) -> DeviceCommand:
        values = {
            "command_id": command.command_id,
            "device_id": command.device_id,
            "operation": command.operation,
            "payload_json": command.payload_json,
            "state": command.state.value,
            "created_at": command.created_at,
            "expires_at": command.expires_at,
            "updated_at": command.updated_at,
            "error": command.error,
            "result_json": command.result_json,
        }
        async with self._database.sessions.begin() as session:
            row = await session.get(CommandRow, command.command_id)
            if row is None:
                session.add(CommandRow(**values))
            else:
                if row.device_id != command.device_id:
                    raise ValueError("command_id belongs to another device")
                for name, value in values.items():
                    setattr(row, name, value)
        return command

    @staticmethod
    def _decode(row: CommandRow) -> DeviceCommand:
        return DeviceCommand(
            command_id=row.command_id,
            device_id=row.device_id,
            operation=row.operation,
            payload_json=row.payload_json,
            state=CommandState(row.state),
            created_at=_aware(row.created_at),
            expires_at=_aware(row.expires_at),
            updated_at=_aware(row.updated_at),
            error=row.error,
            result_json=row.result_json,
        )


class SqlDeviceSessionRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, session_id: str) -> DeviceSessionLease | None:
        async with self._database.sessions() as session:
            row = await session.get(DeviceSessionRow, session_id)
            return None if row is None else self._decode(row)

    async def upsert(self, lease: DeviceSessionLease) -> DeviceSessionLease:
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(DeviceSessionRow)
                .where(DeviceSessionRow.session_id == lease.session_id)
                .with_for_update()
            )
            if row is not None and lease.fencing_token < row.fencing_token:
                raise PermissionError("stale device-session fencing token")
            if row is not None and row.device_id != lease.device_id:
                raise ValueError("session_id belongs to another device")
            values = self._values(lease)
            if row is None:
                session.add(DeviceSessionRow(**values, version=1))
            else:
                for name, value in values.items():
                    setattr(row, name, value)
                row.version += 1
        return lease

    async def active_for_device(
        self, device_id: str, *, now: datetime
    ) -> tuple[DeviceSessionLease, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(DeviceSessionRow).where(
                        DeviceSessionRow.device_id == device_id,
                        DeviceSessionRow.state == DeviceSessionState.ACTIVE.value,
                        DeviceSessionRow.expires_at > now,
                    )
                )
            ).all()
        leases = [self._decode(row) for row in rows]
        if leases:
            newest_epoch = max(item.fencing_token for item in leases)
            leases = [item for item in leases if item.fencing_token == newest_epoch]
        return tuple(sorted(leases, key=lambda item: item.session_id))

    @staticmethod
    def _values(lease: DeviceSessionLease) -> dict[str, object]:
        return {
            "session_id": lease.session_id,
            "device_id": lease.device_id,
            "opened_at": lease.opened_at,
            "renewed_at": lease.renewed_at,
            "expires_at": lease.expires_at,
            "lease_token": lease.lease_token,
            "identity_fingerprint": lease.identity_fingerprint,
            "hub_instance_id": lease.hub_instance_id,
            "fencing_token": lease.fencing_token,
            "heartbeat_sequence": lease.heartbeat_sequence,
            "state": lease.state.value,
        }

    @staticmethod
    def _decode(row: DeviceSessionRow) -> DeviceSessionLease:
        return DeviceSessionLease(
            session_id=row.session_id,
            device_id=row.device_id,
            opened_at=_aware(row.opened_at),
            renewed_at=_aware(row.renewed_at),
            expires_at=_aware(row.expires_at),
            lease_token=row.lease_token,
            identity_fingerprint=row.identity_fingerprint,
            hub_instance_id=row.hub_instance_id,
            fencing_token=row.fencing_token,
            heartbeat_sequence=row.heartbeat_sequence,
            state=DeviceSessionState(row.state),
        )


class SqlDeviceAuthorityRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def validate(
        self,
        *,
        device_id: str,
        hub_instance_id: str,
        fencing_token: int,
        now: datetime,
    ) -> DeviceAuthorityLease:
        async with self._database.sessions() as session:
            row = await session.get(DeviceAuthorityRow, device_id)
            if (
                row is None
                or row.hub_instance_id != hub_instance_id
                or row.fencing_token != fencing_token
                or _aware(row.expires_at) <= now
            ):
                raise AuthorityLeaseHeld(device_id)
            return self._decode(row)

    async def acquire(
        self,
        *,
        device_id: str,
        hub_instance_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> DeviceAuthorityLease:
        self._validate_time(now, ttl)
        for _ in range(8):
            try:
                async with self._database.sessions.begin() as session:
                    row = await session.scalar(
                        select(DeviceAuthorityRow)
                        .where(DeviceAuthorityRow.device_id == device_id)
                        .with_for_update()
                    )
                    if row is None:
                        lease = DeviceAuthorityLease(device_id, hub_instance_id, 1, now + ttl)
                        session.add(
                            DeviceAuthorityRow(
                                device_id=device_id,
                                hub_instance_id=hub_instance_id,
                                fencing_token=1,
                                expires_at=lease.expires_at,
                                version=1,
                            )
                        )
                        return lease
                    expires_at = _aware(row.expires_at)
                    if expires_at > now and row.hub_instance_id != hub_instance_id:
                        raise AuthorityLeaseHeld(device_id)
                    token = row.fencing_token + 1 if expires_at <= now else row.fencing_token
                    row.hub_instance_id = hub_instance_id
                    row.fencing_token = token
                    row.expires_at = now + ttl
                    row.version += 1
                    return self._decode(row)
            except IntegrityError:
                continue
        raise RuntimeError("device authority retry budget exhausted")

    async def renew(
        self,
        *,
        device_id: str,
        hub_instance_id: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> DeviceAuthorityLease:
        self._validate_time(now, ttl)
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(DeviceAuthorityRow)
                .where(DeviceAuthorityRow.device_id == device_id)
                .with_for_update()
            )
            if (
                row is None
                or row.hub_instance_id != hub_instance_id
                or row.fencing_token != fencing_token
            ):
                raise AuthorityLeaseHeld(device_id)
            row.expires_at = now + ttl
            row.version += 1
            return self._decode(row)

    @staticmethod
    def _validate_time(now: datetime, ttl: timedelta) -> None:
        if now.tzinfo is None or ttl <= timedelta(0):
            raise ValueError("aware now and positive authority ttl are required")

    @staticmethod
    def _decode(row: DeviceAuthorityRow) -> DeviceAuthorityLease:
        return DeviceAuthorityLease(
            device_id=row.device_id,
            hub_instance_id=row.hub_instance_id,
            fencing_token=row.fencing_token,
            expires_at=_aware(row.expires_at),
        )


class SqlChallengeRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, challenge_id: str) -> EnrollmentChallenge | None:
        async with self._database.sessions() as session:
            row = await session.get(ChallengeRow, challenge_id)
            return None if row is None else self._decode(row)

    async def create(self, challenge: EnrollmentChallenge) -> None:
        try:
            async with self._database.sessions.begin() as session:
                session.add(ChallengeRow(**self._values(challenge), version=1))
        except IntegrityError as exc:
            raise RuntimeError("challenge already exists") from exc

    async def consume(self, challenge_id: str) -> EnrollmentChallenge:
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(ChallengeRow)
                .where(ChallengeRow.challenge_id == challenge_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(challenge_id)
            if row.consumed:
                raise PermissionError("session challenge already consumed")
            row.consumed = True
            row.version += 1
            return self._decode(row)

    @staticmethod
    def _values(value: EnrollmentChallenge) -> dict[str, object]:
        return {
            "challenge_id": value.challenge_id,
            "device_id": value.device_id,
            "client_nonce": value.client_nonce,
            "server_nonce": value.server_nonce,
            "expires_at": value.expires_at,
            "consumed": value.consumed,
        }

    @staticmethod
    def _decode(row: ChallengeRow) -> EnrollmentChallenge:
        return EnrollmentChallenge(
            challenge_id=row.challenge_id,
            device_id=row.device_id,
            client_nonce=row.client_nonce,
            server_nonce=row.server_nonce,
            expires_at=_aware(row.expires_at),
            consumed=row.consumed,
        )


def _directory_payload(entry: DeviceDirectoryEntry) -> str:
    return json.dumps(
        {
            "device_id": entry.device_id,
            "owner_scope": entry.owner_scope,
            "display_name": entry.display_name,
            "device_kind": entry.device_kind,
            "manifest_json": entry.manifest_json,
            "manifest_revision": entry.manifest_revision,
            "approved": entry.approved,
            "revoked": entry.revoked,
            "online": entry.online,
            "sessions": [
                {
                    "session_id": item.session_id,
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in entry.sessions
            ],
            "registered_at": entry.registered_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
            "revision": entry.revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_directory(payload: str) -> DeviceDirectoryEntry:
    value = json.loads(payload)
    return DeviceDirectoryEntry(
        device_id=value["device_id"],
        owner_scope=value["owner_scope"],
        display_name=value["display_name"],
        device_kind=value["device_kind"],
        manifest_json=value["manifest_json"],
        manifest_revision=value["manifest_revision"],
        approved=value["approved"],
        revoked=value["revoked"],
        online=value["online"],
        sessions=tuple(
            DirectorySession(
                session_id=item["session_id"],
                expires_at=datetime.fromisoformat(item["expires_at"]),
            )
            for item in value["sessions"]
        ),
        registered_at=datetime.fromisoformat(value["registered_at"]),
        updated_at=datetime.fromisoformat(value["updated_at"]),
        revision=value["revision"],
    )


class SqlDeviceDirectoryRepository:
    """Durable source behind the optional in-memory hot projection."""

    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, *, owner_scope: str, device_id: str) -> DeviceDirectoryEntry | None:
        async with self._database.sessions() as session:
            row = await session.get(DirectoryRow, device_id)
            if row is None or row.owner_scope != owner_scope:
                return None
            return _decode_directory(row.payload_json)

    async def upsert(self, entry: DeviceDirectoryEntry) -> DeviceDirectoryEntry:
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(DirectoryRow)
                .where(DirectoryRow.device_id == entry.device_id)
                .with_for_update()
            )
            if row is None:
                candidate = replace(entry, revision=1)
                session.add(
                    DirectoryRow(
                        device_id=entry.device_id,
                        owner_scope=entry.owner_scope,
                        payload_json=_directory_payload(candidate),
                        updated_at=entry.updated_at,
                        revision=1,
                    )
                )
                return candidate
            prior = _decode_directory(row.payload_json)
            if entry.updated_at < prior.updated_at:
                raise RuntimeError("stale Device Directory projection")
            comparable = replace(
                entry,
                revision=prior.revision,
                updated_at=prior.updated_at,
            )
            if comparable == prior:
                return prior
            candidate = replace(entry, revision=prior.revision + 1)
            row.owner_scope = candidate.owner_scope
            row.payload_json = _directory_payload(candidate)
            row.updated_at = candidate.updated_at
            row.revision = candidate.revision
            return candidate

    async def list(self, *, owner_scope: str) -> tuple[DeviceDirectoryEntry, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(DirectoryRow)
                    .where(DirectoryRow.owner_scope == owner_scope)
                    .order_by(DirectoryRow.device_id)
                )
            ).all()
            return tuple(_decode_directory(row.payload_json) for row in rows)

    async def list_all(self) -> tuple[DeviceDirectoryEntry, ...]:
        async with self._database.sessions() as session:
            rows = (await session.scalars(select(DirectoryRow))).all()
            return tuple(_decode_directory(row.payload_json) for row in rows)


class SqlChannelLeaseRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def get(self, channel_id: str) -> ChannelLease | None:
        async with self._database.sessions() as session:
            row = await session.get(ChannelLeaseRow, channel_id)
            return None if row is None else self._decode(row)

    async def upsert(self, lease: ChannelLease) -> ChannelLease:
        async with self._database.sessions.begin() as session:
            row = await session.get(ChannelLeaseRow, lease.channel_id)
            values = self._values(lease)
            if row is None:
                session.add(ChannelLeaseRow(**values))
            else:
                if row.device_id != lease.device_id:
                    raise ValueError("channel_id belongs to another device")
                for name, value in values.items():
                    setattr(row, name, value)
        return lease

    async def active_for_device(
        self,
        device_id: str,
        *,
        now: datetime,
        purpose: str | None = None,
    ) -> tuple[ChannelLease, ...]:
        query = select(ChannelLeaseRow).where(
            ChannelLeaseRow.device_id == device_id,
            ChannelLeaseRow.state == ChannelState.ACTIVE.value,
            ChannelLeaseRow.expires_at > now,
        )
        if purpose is not None:
            query = query.where(ChannelLeaseRow.purpose == purpose)
        async with self._database.sessions() as session:
            rows = (await session.scalars(query)).all()
        return tuple(sorted((self._decode(row) for row in rows), key=lambda item: item.channel_id))

    async def list_for_device(self, device_id: str) -> tuple[ChannelLease, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ChannelLeaseRow)
                    .where(ChannelLeaseRow.device_id == device_id)
                    .order_by(ChannelLeaseRow.channel_id)
                )
            ).all()
        return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _values(lease: ChannelLease) -> dict[str, object]:
        return {
            "channel_id": lease.channel_id,
            "device_id": lease.device_id,
            "purpose": lease.purpose,
            "kinds": json.dumps(sorted(kind.value for kind in lease.kinds)),
            "binding_format": lease.binding_format,
            "issued_at": lease.issued_at,
            "expires_at": lease.expires_at,
            "state": lease.state.value,
            "updated_at": lease.updated_at,
        }

    @staticmethod
    def _decode(row: ChannelLeaseRow) -> ChannelLease:
        return ChannelLease(
            channel_id=row.channel_id,
            device_id=row.device_id,
            purpose=row.purpose,
            kinds=frozenset(ChannelKind(value) for value in json.loads(row.kinds)),
            binding_format=row.binding_format,
            issued_at=_aware(row.issued_at),
            expires_at=_aware(row.expires_at),
            state=ChannelState(row.state),
            updated_at=_aware(row.updated_at) if row.updated_at else None,
        )


class SqlChannelCursorRepository:
    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def next_outbound(self, channel_id: str) -> int:
        for _ in range(8):
            try:
                async with self._database.sessions.begin() as session:
                    row = await session.scalar(
                        select(ChannelCursorRow)
                        .where(ChannelCursorRow.channel_id == channel_id)
                        .with_for_update()
                    )
                    if row is None:
                        session.add(
                            ChannelCursorRow(
                                channel_id=channel_id,
                                outbound_sequence=1,
                                inbound_sequence=0,
                                inbound_envelope_id="",
                                version=1,
                            )
                        )
                        return 1
                    row.outbound_sequence += 1
                    row.version += 1
                    return row.outbound_sequence
            except IntegrityError:
                continue
        raise RuntimeError("channel cursor retry budget exhausted")

    async def accept_inbound(self, *, channel_id: str, sequence: int, envelope_id: str) -> bool:
        if sequence < 1 or not envelope_id:
            raise ValueError("positive sequence and envelope_id are required")
        for _ in range(8):
            try:
                async with self._database.sessions.begin() as session:
                    row = await session.scalar(
                        select(ChannelCursorRow)
                        .where(ChannelCursorRow.channel_id == channel_id)
                        .with_for_update()
                    )
                    if row is None:
                        session.add(
                            ChannelCursorRow(
                                channel_id=channel_id,
                                outbound_sequence=0,
                                inbound_sequence=sequence,
                                inbound_envelope_id=envelope_id,
                                version=1,
                            )
                        )
                        return True
                    if sequence < row.inbound_sequence:
                        raise PermissionError("stale channel envelope sequence")
                    if sequence == row.inbound_sequence:
                        if envelope_id == row.inbound_envelope_id:
                            return False
                        raise PermissionError("channel sequence was reused by another envelope")
                    row.inbound_sequence = sequence
                    row.inbound_envelope_id = envelope_id
                    row.version += 1
                    return True
            except IntegrityError:
                continue
        raise RuntimeError("channel cursor retry budget exhausted")


class SqlEventBus:
    """Durable domain-event log; duplicate event IDs are idempotent."""

    def __init__(self, database: HubDatabase) -> None:
        self._database = database

    async def publish(self, event: DomainEvent) -> None:
        async with self._database.sessions.begin() as session:
            current = await session.scalar(
                select(EventRow).where(EventRow.event_id == event.event_id)
            )
            if current is not None:
                if (
                    current.event_type != event.event_type
                    or current.subject != event.subject
                    or current.data_json != event.data_json
                ):
                    raise ValueError("event_id was reused with different content")
                return
            device = await session.get(DeviceRow, event.subject)
            session.add(
                EventRow(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    source=event.source,
                    subject=event.subject,
                    owner_id=(device.owner_id if device and device.owner_id else "unclaimed"),
                    occurred_at=event.occurred_at,
                    data_json=event.data_json,
                )
            )

    async def list_after(
        self, *, owner_scope: str, stream_position: int, limit: int
    ) -> tuple[StoredDomainEvent, ...]:
        if stream_position < 0:
            raise ValueError("stream_position must not be negative")
        if not 1 <= limit <= 500:
            raise ValueError("event stream limit must be between 1 and 500")
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.stream_position > stream_position,
                        EventRow.owner_id == owner_scope,
                    )
                    .order_by(EventRow.stream_position)
                    .limit(limit)
                )
            ).all()
        return tuple(
            StoredDomainEvent(
                stream_position=row.stream_position,
                event=DomainEvent(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    source=row.source,
                    subject=row.subject,
                    occurred_at=_aware(row.occurred_at),
                    data_json=row.data_json,
                ),
            )
            for row in rows
        )


class SqlHubRepositories:
    """Composition-only bundle; callers receive individual repository ports."""

    def __init__(self, database: HubDatabase) -> None:
        self.devices = SqlDeviceRepository(database)
        self.commands = SqlCommandRepository(database)
        self.sessions = SqlDeviceSessionRepository(database)
        self.authority = SqlDeviceAuthorityRepository(database)
        self.challenges = SqlChallengeRepository(database)
        self.directory = SqlDeviceDirectoryRepository(database)
        self.channel_leases = SqlChannelLeaseRepository(database)
        self.channel_cursors = SqlChannelCursorRepository(database)
        self.events = SqlEventBus(database)
