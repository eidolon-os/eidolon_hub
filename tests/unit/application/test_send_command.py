from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.application.use_cases.send_command import SendCommand
from hub.domain.commands.entities import CommandState
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _Clock:
    def now(self):
        return NOW


class _Ids:
    def new(self, prefix):
        return f"{prefix}-generated"


class _Devices:
    def __init__(self):
        self.device = ManagedDevice(
            identity=DeviceIdentity("device-1", "p256:fingerprint", "local"),
            display_name="Device",
            device_kind="generic",
            manifest=DeviceManifestDocument.from_mapping(
                {
                    "actions": [],
                    "events": [],
                    "media": [],
                    "properties": [],
                    "schema_version": 1,
                    "title": "Device",
                }
            ),
            registered_at=NOW,
            updated_at=NOW,
            approved=True,
        )

    async def get(self, device_id):
        return self.device if device_id == "device-1" else None


class _Commands:
    def __init__(self):
        self.items = {}

    async def get(self, command_id):
        return self.items.get(command_id)

    async def upsert(self, command):
        self.items[command.command_id] = command
        return command


class _Sessions:
    def __init__(self):
        self.online = True

    async def active_for_device(self, device_id, *, now):
        return (object(),) if self.online and device_id == "device-1" else ()


class _Sender:
    def __init__(self):
        self.items = []
        self.error = None

    async def send_command(self, command):
        if self.error:
            raise self.error
        self.items.append(command)


def _use_case():
    devices, sessions, commands, sender = _Devices(), _Sessions(), _Commands(), _Sender()
    return (
        SendCommand(
            devices=devices,
            sessions=sessions,
            commands=commands,
            sender=sender,
            clock=_Clock(),
            ids=_Ids(),
        ),
        devices,
        sessions,
        commands,
        sender,
    )


async def test_command_request_is_durable_and_idempotent() -> None:
    use_case, _devices, _sessions, commands, sender = _use_case()

    first = await use_case.execute(
        device_id="device-1",
        operation="sensor.calibrate",
        payload_json='{"offset":1}',
        request_id="request-command-1",
    )
    retry = await use_case.execute(
        device_id="device-1",
        operation="sensor.calibrate",
        payload_json='{"offset":1}',
        request_id="request-command-1",
    )

    assert first.state is CommandState.SENT
    assert retry == first == commands.items["request-command-1"]
    assert len(sender.items) == 1


async def test_request_id_content_binding_prevents_command_substitution() -> None:
    use_case, _devices, _sessions, _commands, _sender = _use_case()
    await use_case.execute(
        device_id="device-1",
        operation="sensor.calibrate",
        payload_json='{"offset":1}',
        request_id="request-command-1",
    )

    with pytest.raises(ValueError, match="different content"):
        await use_case.execute(
            device_id="device-1",
            operation="sensor.calibrate",
            payload_json='{"offset":2}',
            request_id="request-command-1",
        )


async def test_unapproved_device_and_transport_failure_are_not_hidden() -> None:
    use_case, devices, _sessions, commands, sender = _use_case()
    devices.device = replace(devices.device, approved=False)
    with pytest.raises(PermissionError, match="not approved"):
        await use_case.execute(
            device_id="device-1",
            operation="sensor.calibrate",
            payload_json="{}",
        )

    devices.device = replace(devices.device, approved=True)
    sender.error = ConnectionError("provider unavailable")
    with pytest.raises(ConnectionError, match="provider unavailable"):
        await use_case.execute(
            device_id="device-1",
            operation="sensor.calibrate",
            payload_json="{}",
            request_id="request-failed",
            ttl=timedelta(seconds=10),
        )
    assert commands.items["request-failed"].state is CommandState.FAILED


async def test_offline_device_cannot_receive_a_command() -> None:
    use_case, _devices, sessions, _commands, sender = _use_case()
    sessions.online = False

    with pytest.raises(ConnectionError, match="active session"):
        await use_case.execute(
            device_id="device-1",
            operation="sensor.calibrate",
            payload_json="{}",
        )

    assert sender.items == []
