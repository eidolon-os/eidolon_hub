"""Provider-neutral bridge for standard data-channel envelopes."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx
from pydantic import ValidationError

from hub.application.use_cases.ingest_data_envelope import (
    DataEnvelopeRejected,
    IngestDataEnvelope,
)
from hub.contracts.bindings.channel import CommandDataPayload, DataEnvelope
from hub.contracts.mappers import data_envelope_to_domain
from hub.domain.channels.entities import ChannelKind
from hub.domain.commands.entities import DeviceCommand
from hub.ports.identity import Clock
from hub.ports.repositories import ChannelCursorRepository, ChannelLeaseRepository

logger = logging.getLogger(__name__)


class DataEnvelopeSender(Protocol):
    async def send(self, payload: bytes) -> None: ...


class HttpDataEnvelopeSender:
    """Posts an envelope to the external Provider's standard ingress API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        route: str,
        bearer_token: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not route.startswith(("http://", "https://")):
            raise ValueError("Provider data route must be an HTTP(S) URI")
        self._client = client
        self._route = route
        self._headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        self._timeout = timeout_seconds

    async def send(self, payload: bytes) -> None:
        response = await self._client.post(
            self._route,
            content=payload,
            headers={"Content-Type": "application/json", **self._headers},
            timeout=self._timeout,
        )
        response.raise_for_status()


class ProviderDataChannelBridge:
    """Maps Provider traffic without knowing WSS, LiveKit or media details."""

    def __init__(
        self,
        *,
        sender: DataEnvelopeSender,
        channels: ChannelLeaseRepository,
        cursors: ChannelCursorRepository,
        ingest: IngestDataEnvelope,
        clock: Clock,
        management_purpose: str = "management",
    ) -> None:
        self._sender = sender
        self._channels = channels
        self._cursors = cursors
        self._ingest = ingest
        self._clock = clock
        self._management_purpose = management_purpose

    async def send_command(self, command: DeviceCommand) -> None:
        now = self._clock.now()
        channels = await self._channels.active_for_device(
            command.device_id,
            now=now,
            purpose=self._management_purpose,
        )
        channels = tuple(
            channel for channel in channels if ChannelKind.RELIABLE_DATA in channel.kinds
        )
        if not channels:
            raise ConnectionError("device has no active reliable data channel")
        channel = channels[-1]
        sequence = await self._cursors.next_outbound(channel.channel_id)
        payload = CommandDataPayload(
            command_id=command.command_id,
            operation=command.operation,
            arguments_json=command.payload_json,
            expires_at_ms=int(command.expires_at.timestamp() * 1000),
        )
        envelope = DataEnvelope(
            envelope_id=command.command_id,
            channel_id=channel.channel_id,
            device_id=command.device_id,
            kind="command",
            sequence=sequence,
            occurred_at_ms=int(now.timestamp() * 1000),
            payload_json=payload.model_dump_json(),
        )
        await self._sender.send(envelope.model_dump_json().encode())

    async def ingest_raw(self, raw: bytes) -> bool:
        """Validate, de-duplicate and hand one Provider envelope to the core."""

        envelope_id = "unknown"
        channel_id = "unknown"
        try:
            envelope = DataEnvelope.model_validate_json(raw)
            envelope_id = envelope.envelope_id
            channel_id = envelope.channel_id
            accepted = await self._cursors.accept_inbound(
                channel_id=envelope.channel_id,
                sequence=envelope.sequence,
                envelope_id=envelope.envelope_id,
            )
            if accepted:
                await self._ingest.execute(data_envelope_to_domain(envelope))
            return accepted
        except (ValidationError, ValueError, PermissionError, DataEnvelopeRejected) as exc:
            logger.warning(
                "Rejected channel envelope channel_id=%s envelope_id=%s reason=%s",
                channel_id,
                envelope_id,
                type(exc).__name__,
            )
            raise
