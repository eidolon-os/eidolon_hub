from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from hub.application.queries.get_device import GetDevice
from hub.application.queries.list_devices import DeviceListQuery, ListDevices
from hub.contracts.mappers import directory_entry_to_wire
from hub.domain.devices.entities import (
    DeviceDirectoryEntry,
    DeviceLifecycleState,
)
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _entry(
    device_id: str,
    *,
    lifecycle_state: DeviceLifecycleState = DeviceLifecycleState.APPROVED,
) -> DeviceDirectoryEntry:
    return DeviceDirectoryEntry(
        device_id=device_id,
        owner_scope="owner-1",
        display_name=f"Display {device_id}",
        device_kind="display",
        manifest=DeviceManifestDocument.from_mapping(
            {
                "schema_version": 1,
                "title": "Display",
                "actions": [
                    {
                        "name": "display.render",
                        "version": 1,
                        "input_schema": {},
                        "output_schema": {},
                    }
                ],
            }
        ),
        lifecycle_state=lifecycle_state,
        claim_generation=1,
        trust_epoch=1,
        enrolled_at=NOW,
        updated_at=NOW,
    )


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _Directory:
    def __init__(self) -> None:
        self.entries = (
            _entry("device-a"),
            replace(_entry("device-b"), device_kind="sensor"),
            _entry("device-c"),
        )

    async def get(self, *, owner_scope, device_id):
        return next(
            (
                entry
                for entry in self.entries
                if entry.owner_scope == owner_scope and entry.device_id == device_id
            ),
            None,
        )

    async def list(self, *, owner_scope):
        return tuple(entry for entry in self.entries if entry.owner_scope == owner_scope)


def test_directory_wire_accepts_deployed_mac_style_device_id() -> None:
    entry = replace(
        _entry("10:51:db:7e:24:44"),
        owner_scope="business_owner_account_1",
        owner_domain_id="owner-domain_01",
    )
    wire = directory_entry_to_wire(entry)

    assert wire.device_ref is not None
    assert wire.device_ref.device_instance_id == "10:51:db:7e:24:44"
    assert str(wire.device_ref.owner_domain_id) == "owner-domain_01"
    assert str(wire.device_ref.owner_domain_id) != wire.owner_scope


@pytest.mark.asyncio
async def test_get_device_is_owner_scoped_and_exact() -> None:
    query = GetDevice(_Directory())

    assert (
        await query.execute(owner_scope="owner-1", device_id="device-a")
    ).device_id == "device-a"
    with pytest.raises(KeyError):
        await query.execute(owner_scope="other-owner", device_id="device-a")


@pytest.mark.asyncio
async def test_list_devices_combines_structured_filters_and_stable_cursor() -> None:
    query = ListDevices(_Directory(), clock=_Clock())

    first = await query.execute(
        DeviceListQuery(
            owner_scope="owner-1",
            lifecycle_state=DeviceLifecycleState.APPROVED,
            device_kind="display",
            capability="display.render",
            limit=1,
        )
    )
    second = await query.execute(
        DeviceListQuery(
            owner_scope="owner-1",
            lifecycle_state=DeviceLifecycleState.APPROVED,
            device_kind="display",
            capability="display.render",
            after=first.next_cursor,
            limit=1,
        )
    )

    assert [entry.device_id for entry in first.entries] == ["device-a"]
    assert first.next_cursor == "device-a"
    assert [entry.device_id for entry in second.entries] == ["device-c"]
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_list_q_is_a_bounded_ui_filter_not_a_separate_search_contract() -> None:
    directory = _Directory()
    directory.entries = (replace(_entry("device-a"), display_name="Kitchen Display"),)

    page = await ListDevices(directory, clock=_Clock()).execute(
        DeviceListQuery(owner_scope="owner-1", q=" kitchen ", limit=20)
    )

    assert [entry.device_id for entry in page.entries] == ["device-a"]


@pytest.mark.parametrize(
    "query",
    (
        DeviceListQuery(owner_scope="owner-1", limit=1),
        DeviceListQuery(owner_scope="owner-1", limit=100),
    ),
)
def test_list_query_accepts_only_bounded_pages(query) -> None:
    assert 1 <= query.limit <= 100


@pytest.mark.parametrize(
    "values",
    (
        {"owner_scope": ""},
        {"owner_scope": "owner-1", "limit": 0},
        {"owner_scope": "owner-1", "limit": 101},
        {"owner_scope": "owner-1", "q": "x" * 129},
    ),
)
def test_list_query_rejects_unbounded_or_ambiguous_inputs(values) -> None:
    with pytest.raises(ValueError):
        DeviceListQuery(**values)
