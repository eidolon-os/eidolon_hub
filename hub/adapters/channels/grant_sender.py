"""Relay opaque channel grants over whichever connection is preferred."""

from __future__ import annotations

import base64
import json
from typing import Protocol

from hub.domain.channels.entities import ChannelGrant


class SignalingTransport(Protocol):
    async def send(self, *, signaling_ref: str, payload: bytes) -> None: ...


class MailboxWriter(Protocol):
    async def put(self, *, signaling_ref: str, payload: bytes) -> None: ...


class MqttSignalWriter(Protocol):
    async def send_signal(self, *, device_id: str, payload: bytes) -> None: ...


class GrantSignalingRouter:
    def __init__(self, transports: dict[str, SignalingTransport]) -> None:
        self._transports = dict(transports)

    async def send_grant(self, *, signaling_ref: str, grant: ChannelGrant) -> None:
        prefix = signaling_ref.partition(":")[0]
        try:
            transport = self._transports[prefix]
        except KeyError as exc:
            raise RuntimeError(f"no signaling transport for {prefix!r}") from exc
        # Encoding is a wire operation only; the provider-owned binding bytes
        # are never interpreted, logged or stored.
        payload = json.dumps(
            {
                "operation": "channel.grant",
                "request_id": grant.request_id,
                "channel_id": grant.lease.channel_id,
                "profile_name": grant.lease.profile_name,
                "lease_expires_at_ms": int(grant.lease.expires_at.timestamp() * 1000),
                "opaque_binding": base64.b64encode(grant.opaque_binding.relay_bytes()).decode(
                    "ascii"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        await transport.send(signaling_ref=signaling_ref, payload=payload)


class HttpMailboxTransport:
    def __init__(self, mailbox: MailboxWriter) -> None:
        self._mailbox = mailbox

    async def send(self, *, signaling_ref: str, payload: bytes) -> None:
        await self._mailbox.put(signaling_ref=signaling_ref, payload=payload)


class MqttSignalingTransport:
    def __init__(self, connector: MqttSignalWriter) -> None:
        self._connector = connector

    async def send(self, *, signaling_ref: str, payload: bytes) -> None:
        prefix = "mqtt:"
        if not signaling_ref.startswith(prefix):
            raise ValueError("not an MQTT signaling reference")
        await self._connector.send_signal(device_id=signaling_ref[len(prefix) :], payload=payload)
