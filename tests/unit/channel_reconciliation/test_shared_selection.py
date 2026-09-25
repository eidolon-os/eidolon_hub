from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.control.shared_session import SharedSessionSelection
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from test_channel_reconciliation import _device

from hub.channel_reconciliation.shared_selection import ResolveSharedSelection
from hub.domain.devices.entities import DeviceLifecycleState
from hub.domain.devices.identity import DeviceIdentity
from hub.ports.identity import ManagementPrincipal


def setup():
    first = _device()
    second = replace(first, identity=DeviceIdentity(named_device_instance_id("shared-other")))
    selection = SharedSessionSelection(
        session_id="visit-1",
        devices=(first.device_ref, second.device_ref),
        input_device_id=first.identity.device_id,
    )
    principal = ManagementPrincipal("owner-session", "owner_01", frozenset({"owner"}))
    return first, second, selection, principal


async def test_resolves_only_existing_projection_without_binding_or_operation_state():
    first, second, selection, principal = setup()
    reader = SimpleNamespace(get=AsyncMock(side_effect=[first, second]))
    result = await ResolveSharedSelection(reader).execute(selection, principal=principal)
    assert result["owner_id"] == "owner_01"
    assert len(result["specifications"]) == 2
    for item in result["specifications"]:
        assert set(item) == {"device_ref", "device"}
        assert item["device"]["manifest_revision"] == first.manifest_digest
        assert "channel" not in item


@pytest.mark.parametrize("case", ["unscoped", "foreign", "missing", "revoked", "generation"])
async def test_refuses_incomplete_or_unauthorized_selection(case):
    first, second, selection, principal = setup()
    if case == "unscoped":
        principal = replace(principal, owner_id=None)
    if case == "foreign":
        second = replace(second, owner_id="another")
    if case == "missing":
        second = None
    if case == "revoked":
        second = replace(second, lifecycle_state=DeviceLifecycleState.REVOKED)
    if case == "generation":
        second = replace(second, trust_epoch=second.trust_epoch + 1)
    reader = SimpleNamespace(get=AsyncMock(side_effect=[first, second]))
    with pytest.raises(PermissionError):
        await ResolveSharedSelection(reader).execute(selection, principal=principal)
    if case == "unscoped":
        reader.get.assert_not_called()
