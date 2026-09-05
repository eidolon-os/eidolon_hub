"""Ports for business Channel reconciliation, outside Admission."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from hub.contracts.bindings.device import DeviceRef
from hub.domain.devices.entities import ManagedDevice

from .domain import ChannelBinding, ChannelRevocationDelivery, CurrentChannelBinding


class ChannelBindingProvider(Protocol):
    async def current(self, *, device_ref: DeviceRef) -> CurrentChannelBinding | None: ...

    async def provision(
        self,
        *,
        operation_id: str,
        device_ref: DeviceRef,
        owner_id: str,
        display_name: str,
        manifest_id: str,
        manifest: Mapping[str, object],
        manifest_revision: str,
    ) -> tuple[ChannelBinding, ...]: ...

    async def refresh(
        self,
        *,
        operation_id: str,
        device_ref: DeviceRef,
        owner_id: str,
        display_name: str,
        manifest_id: str,
        manifest: Mapping[str, object],
        manifest_revision: str,
    ) -> tuple[ChannelBinding, ...]: ...

    async def revoke(
        self,
        *,
        operation_id: str,
        device_ref: DeviceRef,
        reason: str,
    ) -> None: ...


class ChannelDeviceProjectionReader(Protocol):
    async def get(self, device_id: str) -> ManagedDevice | None: ...


class ChannelRevocationStore(Protocol):
    async def materialize_claim_events(self, *, now: datetime) -> int: ...

    async def list_due(
        self, *, now: datetime, limit: int
    ) -> tuple[ChannelRevocationDelivery, ...]: ...

    async def mark_terminal(
        self,
        *,
        source_event_id: str,
        state: str,
        result_code: str,
        delivered_at: datetime,
    ) -> None: ...

    async def mark_retry(
        self,
        *,
        source_event_id: str,
        attempt_count: int,
        next_attempt_at: datetime,
        error: str,
    ) -> None: ...
