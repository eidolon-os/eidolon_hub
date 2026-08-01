from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.authenticate_connection import AuthenticateConnection
from hub.application.use_cases.close_connection import CloseConnection
from hub.application.use_cases.enroll_device import EnrollDevice, EnrollmentHello
from hub.application.use_cases.get_command import GetCommand
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_connection import RenewConnection
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.application.use_cases.send_command import SendCommand
from hub.domain.commands.entities import CommandState, DeviceCommand
from hub.domain.connections.entities import (
    ConnectionLease,
    ConnectorKind,
    DeviceAuthorityLease,
)
from hub.domain.devices.entities import DeviceRegistrationIntent, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument
from hub.ports.identity import EnrollmentChallenge

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Ids:
    count = 0

    def new(self, prefix):
        self.count += 1
        return f"{prefix}-{self.count}"


class _Recorder:
    def __init__(self):
        self.values = []

    async def publish(self, value):
        self.values.append(value)

    async def execute(self, value, **kwargs):
        self.values.append((value, kwargs) if kwargs else value)


class _Devices:
    def __init__(self, device=None):
        self.values = {} if device is None else {device.identity.device_id: device}

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, value):
        self.values[value.identity.device_id] = value
        return value


class _Commands:
    def __init__(self, command=None):
        self.values = {} if command is None else {command.command_id: command}

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, value):
        self.values[value.command_id] = value
        return value


class _Connections:
    def __init__(self, *leases):
        self.values = {lease.connection_id: lease for lease in leases}

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, value):
        self.values[value.connection_id] = value
        return value

    async def active_for_device(self, device_id, *, now):
        return tuple(
            value
            for value in self.values.values()
            if value.device_id == device_id and value.is_active(now)
        )


class _Authority:
    def __init__(self):
        self.calls = []

    async def acquire(self, **values):
        self.calls.append(("acquire", values))
        return DeviceAuthorityLease(
            values["device_id"], values["hub_instance_id"], 1, values["now"] + values["ttl"]
        )

    async def renew(self, **values):
        self.calls.append(("renew", values))
        return DeviceAuthorityLease(
            values["device_id"],
            values["hub_instance_id"],
            values["fencing_token"],
            values["now"] + values["ttl"],
        )

    async def validate(self, **values):
        self.calls.append(("validate", values))
        return DeviceAuthorityLease(
            values["device_id"],
            values["hub_instance_id"],
            values["fencing_token"],
            values["now"] + timedelta(seconds=1),
        )


class _Challenges:
    def __init__(self, challenge=None):
        self.value = challenge

    async def create(self, value):
        self.value = value

    async def get(self, key):
        return self.value if self.value is not None and self.value.challenge_id == key else None

    async def consume(self, key):
        self.value = replace(self.value, consumed=True)
        return self.value


class _Proof:
    async def verify(self, **kwargs):
        return "p256:fingerprint"


class _Issuer:
    def issue_lease_token(self, **kwargs):
        return "issued-lease-token"


class _Sender:
    def __init__(self, error=None):
        self.error = error
        self.values = []

    async def send_grant(self, **values):
        if self.error:
            raise self.error
        self.values.append(values)

    async def send_command(self, command):
        if self.error:
            raise self.error
        self.values.append(command)


def _device(**changes) -> ManagedDevice:
    values = {
        "identity": DeviceIdentity("device-1", "p256:fingerprint"),
        "display_name": "Device",
        "device_kind": "generic",
        "manifest": DeviceManifestDocument.from_mapping({"schema_version": 1}),
        "registered_at": NOW,
        "updated_at": NOW,
        "approved": True,
    }
    values.update(changes)
    return ManagedDevice(**values)


def _connection(**changes) -> ConnectionLease:
    values = {
        "connection_id": "connection-1",
        "device_id": "device-1",
        "connector_id": "https-local",
        "connector_kind": ConnectorKind.HTTPS,
        "signaling_ref": "http-mailbox:device-1",
        "opened_at": NOW,
        "renewed_at": NOW,
        "expires_at": NOW + timedelta(seconds=45),
        "lease_token": "lease-token-device-1",
        "identity_fingerprint": "p256:fingerprint",
        "hub_instance_id": "hub-1",
        "fencing_token": 1,
    }
    values.update(changes)
    return ConnectionLease(**values)


def _command(**changes) -> DeviceCommand:
    values = {
        "command_id": "command-1",
        "device_id": "device-1",
        "operation": "display.render",
        "payload_json": "{}",
        "state": CommandState.SENT,
        "created_at": NOW,
        "expires_at": NOW + timedelta(seconds=30),
        "updated_at": NOW,
    }
    values.update(changes)
    return DeviceCommand(**values)


async def test_authenticate_connection_checks_every_lease_property() -> None:
    valid = _connection()
    use_case = AuthenticateConnection(connections=_Connections(valid), clock=_Clock())
    assert (
        await use_case.execute(
            connection_id=valid.connection_id,
            device_id=valid.device_id,
            lease_token=valid.lease_token,
        )
        == valid
    )
    for connections, device_id, token in (
        (_Connections(), "device-1", valid.lease_token),
        (_Connections(valid), "device-2", valid.lease_token),
        (_Connections(valid), "device-1", "wrong"),
        (
            _Connections(
                _connection(
                    opened_at=NOW - timedelta(seconds=60),
                    renewed_at=NOW - timedelta(seconds=60),
                    expires_at=NOW - timedelta(seconds=1),
                )
            ),
            "device-1",
            valid.lease_token,
        ),
    ):
        with pytest.raises(PermissionError, match="active"):
            await AuthenticateConnection(connections=connections, clock=_Clock()).execute(
                connection_id=valid.connection_id,
                device_id=device_id,
                lease_token=token,
            )


async def test_get_command_and_directory_missing_values_are_explicit() -> None:
    command = _command()
    assert await GetCommand(_Commands(command)).execute(command.command_id) == command
    with pytest.raises(KeyError):
        await GetCommand(_Commands()).execute("missing")
    with pytest.raises(KeyError):
        await ProjectDeviceDirectory(
            devices=_Devices(),
            connections=_Connections(),
            directory=_Recorder(),
            clock=_Clock(),
        ).execute("missing")


async def test_approval_and_close_reject_invalid_or_missing_devices_and_leases() -> None:
    approve = ApproveDevice(
        devices=_Devices(), events=_Recorder(), clock=_Clock(), directory_projector=_Recorder()
    )
    with pytest.raises(ValueError, match="owner_id"):
        await approve.execute(device_id="device-1", owner_id="", request_id="request-1")
    with pytest.raises(KeyError):
        await approve.execute(device_id="missing", owner_id="owner-1", request_id="request-1")
    approve = ApproveDevice(
        devices=_Devices(_device(revoked=True)),
        events=_Recorder(),
        clock=_Clock(),
        directory_projector=_Recorder(),
    )
    with pytest.raises(ValueError, match="revoked"):
        await approve.execute(device_id="device-1", owner_id="owner-1", request_id="request-1")

    close = CloseConnection(
        connections=_Connections(),
        events=_Recorder(),
        clock=_Clock(),
        directory_projector=_Recorder(),
    )
    with pytest.raises(KeyError):
        await close.execute(connection_id="missing", device_id="device-1", lease_token="x")
    lease = _connection()
    close._connections = _Connections(lease)
    with pytest.raises(PermissionError, match="does not match"):
        await close.execute(
            connection_id=lease.connection_id, device_id="device-2", lease_token=lease.lease_token
        )
    closed = lease.close()
    close._connections = _Connections(closed)
    assert (
        await close.execute(
            connection_id=closed.connection_id,
            device_id=closed.device_id,
            lease_token=closed.lease_token,
        )
        is closed
    )


async def test_enrollment_rejects_short_unknown_expired_and_mismatched_challenges() -> None:
    challenges = _Challenges()
    enroll = EnrollDevice(
        challenges=challenges,
        connections=_Connections(),
        authority=_Authority(),
        proof_verifier=_Proof(),
        credential_issuer=_Issuer(),
        clock=_Clock(),
        ids=_Ids(),
        hub_instance_id="hub-1",
    )
    with pytest.raises(ValueError, match="at least 16"):
        await enroll.begin(
            EnrollmentHello(
                "device-1", "https-local", ConnectorKind.HTTPS, "http-mailbox:device-1", "short"
            )
        )
    with pytest.raises(PermissionError, match="unknown"):
        await enroll.complete(
            challenge_id="missing",
            expected_device_id="device-1",
            public_key="key",
            signature="signature",
        )
    base = EnrollmentChallenge(
        "challenge-1",
        "device-1",
        "0123456789abcdef",
        "fedcba9876543210",
        NOW + timedelta(seconds=1),
        "https-local",
        "https",
        "http-mailbox:device-1",
        100,
    )
    for challenge, expected, message in (
        (replace(base, consumed=True), "device-1", "unknown"),
        (replace(base, expires_at=NOW), "device-1", "expired"),
        (base, "device-2", "mismatch"),
    ):
        enroll._challenges = _Challenges(challenge)
        with pytest.raises(PermissionError, match=message):
            await enroll.complete(
                challenge_id=challenge.challenge_id,
                expected_device_id=expected,
                public_key="key",
                signature="signature",
            )


async def test_registration_authentication_identity_and_projection_boundaries() -> None:
    identity = DeviceIdentity("device-1", "p256:fingerprint")
    intent = DeviceRegistrationIntent(
        "register-1", identity, "Device", "generic", _device().manifest
    )
    events, projector = _Recorder(), _Recorder()
    for connections, token in (
        (_Connections(), "lease-token-device-1"),
        (_Connections(_connection()), "wrong-token"),
    ):
        with pytest.raises(PermissionError):
            await RegisterDevice(
                devices=_Devices(),
                connections=connections,
                events=events,
                clock=_Clock(),
                directory_projector=projector,
            ).execute(connection_id="connection-1", lease_token=token, registration=intent)
    devices = _Devices(_device(identity=DeviceIdentity("device-1", "p256:another-fingerprint")))
    with pytest.raises(PermissionError, match="cannot be replaced"):
        await RegisterDevice(
            devices=devices,
            connections=_Connections(_connection()),
            events=events,
            clock=_Clock(),
        ).execute(
            connection_id="connection-1",
            lease_token="lease-token-device-1",
            registration=intent,
        )
    devices = _Devices()
    await RegisterDevice(
        devices=devices,
        connections=_Connections(_connection()),
        events=events,
        clock=_Clock(),
        directory_projector=projector,
    ).execute(
        connection_id="connection-1",
        lease_token="lease-token-device-1",
        registration=intent,
    )
    assert projector.values == ["device-1"]


async def test_connection_renewal_guards_and_idempotency() -> None:
    authority, events, projector = _Authority(), _Recorder(), _Recorder()
    renew = RenewConnection(
        connections=_Connections(),
        authority=authority,
        events=events,
        clock=_Clock(),
        ttl=timedelta(seconds=45),
        directory_projector=projector,
    )
    with pytest.raises(KeyError):
        await renew.execute(connection_id="missing", lease_token="x", sequence=1)
    lease = _connection(heartbeat_sequence=1)
    renew._connections = _Connections(lease)
    with pytest.raises(PermissionError):
        await renew.execute(connection_id=lease.connection_id, lease_token="wrong", sequence=2)
    assert (
        await renew.execute(
            connection_id=lease.connection_id, lease_token=lease.lease_token, sequence=1
        )
        is lease
    )
    renewed = await renew.execute(
        connection_id=lease.connection_id, lease_token=lease.lease_token, sequence=2
    )
    assert renewed.heartbeat_sequence == 2
    assert projector.values == ["device-1"]


async def test_revoke_device_replay_semantics() -> None:
    devices = _Devices()
    revoke = RevokeDevice(
        devices=devices,
        connections=_Connections(),
        events=_Recorder(),
        clock=_Clock(),
        directory_projector=_Recorder(),
    )
    with pytest.raises(KeyError):
        await revoke.execute(device_id="missing", reason="test", request_id="revoke-1")
    devices.values["device-1"] = _device(
        last_management_request_id="revoke-1",
        last_management_fingerprint="revoke:test",
    )
    replay = await revoke.execute(device_id="device-1", reason="test", request_id="revoke-1")
    assert replay.revoked is False
    with pytest.raises(ValueError, match="reused"):
        await revoke.execute(device_id="device-1", reason="other", request_id="revoke-1")


@pytest.mark.parametrize(
    ("payload", "ttl", "message"),
    [
        ("{", timedelta(seconds=1), "valid JSON"),
        ("[]", timedelta(seconds=1), "JSON object"),
        ("{}", timedelta(0), "between zero"),
        ("{}", timedelta(minutes=6), "between zero"),
    ],
)
async def test_send_command_rejects_bad_payload_and_ttl(payload, ttl, message) -> None:
    with pytest.raises(ValueError, match=message):
        await SendCommand(
            devices=_Devices(_device()),
            commands=_Commands(),
            sender=_Sender(),
            clock=_Clock(),
            ids=_Ids(),
        ).execute(
            device_id="device-1",
            operation="display.render",
            payload_json=payload,
            ttl=ttl,
        )


async def test_send_command_rejects_revoked_and_resumes_queued_request() -> None:
    with pytest.raises(KeyError):
        await SendCommand(
            devices=_Devices(_device(revoked=True)),
            commands=_Commands(),
            sender=_Sender(),
            clock=_Clock(),
            ids=_Ids(),
        ).execute(device_id="device-1", operation="display.render", payload_json="{}")
    queued = _command(state=CommandState.QUEUED)
    commands, sender = _Commands(queued), _Sender()
    sent = await SendCommand(
        devices=_Devices(_device()),
        commands=commands,
        sender=sender,
        clock=_Clock(),
        ids=_Ids(),
    ).execute(
        device_id="device-1",
        operation=queued.operation,
        payload_json=queued.payload_json,
        request_id=queued.command_id,
        ttl=queued.expires_at - queued.created_at,
    )
    assert sent.state is CommandState.SENT
