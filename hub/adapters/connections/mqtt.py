"""MQTT5 connection/signaling adapter with a deliberately narrow ACL surface."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable

import aiomqtt
from pydantic import Field, TypeAdapter, ValidationError

from hub.application.use_cases.authenticate_connection import AuthenticateConnection
from hub.application.use_cases.close_connection import CloseConnection
from hub.application.use_cases.enroll_device import EnrollDevice, EnrollmentHello
from hub.application.use_cases.handle_channel_signal import HandleChannelSignal
from hub.application.use_cases.register_device import RegisterDevice
from hub.application.use_cases.renew_connection import RenewConnection
from hub.contracts.bindings.channel import ChannelNegotiationSignal
from hub.contracts.bindings.connection import (
    ConnectionClosed,
    ConnectionHeartbeat,
    ConnectionHello,
    ConnectionProof,
    ConnectionRegistration,
)
from hub.contracts.mappers import channel_signal_to_domain, registration_to_domain
from hub.domain.connections.entities import ConnectorKind

logger = logging.getLogger(__name__)

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOPIC_RE = re.compile(r"^eidolon/v1/devices/([A-Za-z0-9_-]{1,128})/connection/in$")
_ALLOWED_OPERATIONS = frozenset(
    {
        "connection.hello",
        "connection.proof",
        "connection.registration",
        "connection.heartbeat",
        "connection.closed",
        "channel.offer",
        "channel.accept",
        "channel.close",
    }
)

MqttInboundContract = Annotated[
    ConnectionHello
    | ConnectionProof
    | ConnectionRegistration
    | ConnectionHeartbeat
    | ConnectionClosed
    | ChannelNegotiationSignal,
    Field(discriminator="operation"),
]
_CONTRACT_ADAPTER = TypeAdapter(MqttInboundContract)


class MqttContractRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MqttInboundMessage:
    device_id: str
    topic: str
    contract: MqttInboundContract


class MqttConnectionCodec:
    @staticmethod
    def decode(topic: str, payload: bytes) -> MqttInboundMessage:
        topic_match = _TOPIC_RE.fullmatch(topic)
        if topic_match is None:
            raise MqttContractRejected("MQTT topic is outside the connection namespace")
        if len(payload) > 128 * 1024:
            raise MqttContractRejected("MQTT connection payload exceeds 128KiB")
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MqttContractRejected("MQTT payload must be a JSON object") from exc
        if not isinstance(raw, dict) or raw.get("operation") not in _ALLOWED_OPERATIONS:
            raise MqttContractRejected("MQTT operation is not allowed")
        try:
            contract = _CONTRACT_ADAPTER.validate_json(payload)
        except ValidationError as exc:
            raise MqttContractRejected("MQTT payload violates its contract") from exc
        device_id = topic_match.group(1)
        payload_device_id = getattr(contract, "device_id", None)
        registration = getattr(contract, "registration", None)
        if registration is not None:
            payload_device_id = registration.identity.device_id
        if payload_device_id is not None and payload_device_id != device_id:
            raise MqttContractRejected("MQTT topic device does not match payload device")
        if isinstance(contract, ConnectionClosed) and contract.reason != "client":
            raise MqttContractRejected("devices may only send client disconnects")
        return MqttInboundMessage(device_id=device_id, topic=topic, contract=contract)

    @staticmethod
    def outbound_topic(device_id: str) -> str:
        if _DEVICE_ID_RE.fullmatch(device_id) is None:
            raise ValueError("invalid MQTT device_id")
        return f"eidolon/v1/devices/{device_id}/connection/out"


InboundHandler = Callable[[MqttInboundMessage], Awaitable[bytes | None]]


def create_mqtt_application_handler(
    *,
    enroll: EnrollDevice,
    register: RegisterDevice,
    renew: RenewConnection,
    close: CloseConnection,
    authenticate_connection: AuthenticateConnection,
    handle_channel_signal: HandleChannelSignal,
    expected_connector_id: str,
    heartbeat_after_ms: int = 15_000,
    connector_priority: int = 100,
) -> InboundHandler:
    async def handle(message: MqttInboundMessage) -> bytes | None:
        contract = message.contract
        if isinstance(contract, ConnectionHello):
            if contract.connector_id != expected_connector_id:
                raise MqttContractRejected("connection hello connector_id does not match binding")
            challenge = await enroll.begin(
                EnrollmentHello(
                    device_id=message.device_id,
                    connector_id=expected_connector_id,
                    connector_kind=ConnectorKind.MQTT5,
                    signaling_ref=f"mqtt:{message.device_id}",
                    client_nonce=contract.client_nonce,
                    priority=connector_priority,
                )
            )
            return json.dumps(
                {
                    "operation": "connection.challenge",
                    "request_id": contract.request_id,
                    "challenge_id": challenge.challenge_id,
                    "server_nonce": challenge.server_nonce,
                    "expires_at_ms": int(challenge.expires_at.timestamp() * 1000),
                },
                separators=(",", ":"),
            ).encode()
        if isinstance(contract, ConnectionProof):
            lease, _ = await enroll.complete(
                challenge_id=contract.challenge_id,
                expected_device_id=contract.device_id,
                public_key=contract.public_key,
                signature=contract.signature,
            )
            return json.dumps(
                {
                    "operation": "connection.accepted",
                    "request_id": contract.request_id,
                    "connection_id": lease.connection_id,
                    "lease_token": lease.lease_token,
                    "lease_expires_at_ms": int(lease.expires_at.timestamp() * 1000),
                    "heartbeat_after_ms": heartbeat_after_ms,
                    "registration_required": True,
                },
                separators=(",", ":"),
            ).encode()
        if isinstance(contract, ConnectionRegistration):
            await register.execute(
                connection_id=contract.connection_id,
                lease_token=contract.lease_token,
                registration=registration_to_domain(contract.registration),
            )
            return None
        if isinstance(contract, ConnectionHeartbeat):
            lease = await renew.execute(
                connection_id=contract.connection_id,
                lease_token=contract.lease_token,
                sequence=contract.sequence,
            )
            return json.dumps(
                {
                    "operation": "connection.accepted",
                    "request_id": contract.request_id,
                    "connection_id": lease.connection_id,
                    "lease_token": lease.lease_token,
                    "lease_expires_at_ms": int(lease.expires_at.timestamp() * 1000),
                    "heartbeat_after_ms": heartbeat_after_ms,
                    "registration_required": False,
                },
                separators=(",", ":"),
            ).encode()
        if isinstance(contract, ConnectionClosed):
            await close.execute(
                connection_id=contract.connection_id,
                device_id=contract.device_id,
                lease_token=contract.lease_token,
            )
            return None
        await authenticate_connection.execute(
            connection_id=contract.connection_id,
            device_id=contract.device_id,
            lease_token=contract.lease_token,
        )
        await handle_channel_signal.execute(channel_signal_to_domain(contract))
        return None

    return handle


class Mqtt5Connector:
    """Long-lived MQTT5 subscription; it never exposes a business-data send API."""

    def __init__(
        self,
        *,
        connector_id: str,
        hostname: str,
        port: int,
        handler: InboundHandler,
        username: str | None = None,
        password: str | None = None,
        tls_context: object | None = None,
        reconnect_seconds: float = 1.0,
        client_id: str | None = None,
    ) -> None:
        self._connector_id = connector_id
        self._hostname = hostname
        self._port = port
        self._handler = handler
        self._username = username
        self._password = password
        self._tls_context = tls_context
        self._reconnect_seconds = reconnect_seconds
        self._client_id = client_id or f"eidolon-hub-{connector_id}"
        self._task: asyncio.Task[None] | None = None
        self._client: aiomqtt.Client | None = None

    @property
    def connector_id(self) -> str:
        return self._connector_id

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"mqtt:{self._connector_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(
                    self._hostname,
                    self._port,
                    username=self._username,
                    password=self._password,
                    identifier=self._client_id,
                    protocol=aiomqtt.ProtocolVersion.V5,
                    tls_context=self._tls_context,  # type: ignore[arg-type]
                ) as client:
                    self._client = client
                    await client.subscribe("eidolon/v1/devices/+/connection/in", qos=1)
                    async for message in client.messages:
                        try:
                            inbound = MqttConnectionCodec.decode(
                                str(message.topic), bytes(message.payload)
                            )
                            response = await self._handler(inbound)
                            if response is not None:
                                await client.publish(
                                    MqttConnectionCodec.outbound_topic(inbound.device_id),
                                    response,
                                    qos=1,
                                )
                        except MqttContractRejected as exc:
                            logger.warning("Rejected MQTT connection message: %s", exc)
                        except (KeyError, PermissionError, ValueError) as exc:
                            logger.warning(
                                "Rejected authenticated MQTT operation device_id=%s reason=%s",
                                inbound.device_id,
                                type(exc).__name__,
                            )
                        except Exception:
                            logger.exception(
                                "MQTT application handler failed device_id=%s",
                                inbound.device_id,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MQTT connector failed; reconnecting")
                await asyncio.sleep(self._reconnect_seconds)
            finally:
                self._client = None

    async def send_signal(self, *, device_id: str, payload: bytes) -> None:
        """Send only prevalidated connection/channel-signaling payloads."""
        raw = json.loads(payload)
        operation = raw.get("operation") if isinstance(raw, dict) else None
        allowed_outbound = {
            "connection.challenge",
            "connection.accepted",
            "connection.closed",
            "channel.grant",
        }
        if operation not in allowed_outbound:
            raise MqttContractRejected("outbound MQTT operation is not allowed")
        if self._client is None:
            raise ConnectionError("MQTT connector is not connected")
        await self._client.publish(MqttConnectionCodec.outbound_topic(device_id), payload, qos=1)
