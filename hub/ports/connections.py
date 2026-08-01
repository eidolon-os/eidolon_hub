"""Ports for connection signaling and connector conformance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ConnectionSignal:
    operation: str
    request_id: str
    payload_json: str


class ConnectionSignalSender(Protocol):
    async def send(self, *, signaling_ref: str, signal: ConnectionSignal) -> None: ...


class ConnectionConnector(Protocol):
    @property
    def connector_id(self) -> str: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class ConnectorSupervisor:
    """Lifecycle composition for any number of independently failing connectors."""

    def __init__(self, connectors: tuple[ConnectionConnector, ...]) -> None:
        ids = [connector.connector_id for connector in connectors]
        if len(ids) != len(set(ids)):
            raise ValueError("connector_id values must be unique")
        self._connectors = connectors

    async def start(self) -> None:
        started: list[ConnectionConnector] = []
        try:
            for connector in self._connectors:
                await connector.start()
                started.append(connector)
        except Exception:
            for connector in reversed(started):
                await connector.stop()
            raise

    async def stop(self) -> None:
        for connector in reversed(self._connectors):
            await connector.stop()
