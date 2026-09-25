from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.control.shared_session import SharedSessionSelection
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from test_channel_reconciliation import _device

from hub.admission.domain import ActorContext, AdmissionProblem
from hub.channel_reconciliation.shared_selection import SharedDeviceSessions
from hub.contracts.bindings.admission import BusinessOwnerId, ControllerActorRef, OwnerDomainId
from hub.domain.devices.entities import DeviceLifecycleState
from hub.domain.devices.identity import DeviceIdentity


def setup():
    first = _device()
    second = replace(first, identity=DeviceIdentity(named_device_instance_id("shared-other")))
    selection = SharedSessionSelection(
        session_id="visit-1",
        devices=(first.device_ref, second.device_ref),
        input_device_id=first.identity.device_id,
    )
    principal = ActorContext(
        actor=ControllerActorRef(
            principal_id="controller-1",
            owner_domain_id=first.owner_domain_id,
            granted_scopes=("device.shared-session.control",),
            authentication_strength="software",
        ),
        owner_domain_id=OwnerDomainId(first.owner_domain_id),
        business_owner_id=BusinessOwnerId("owner_01"),
    )
    return first, second, selection, principal


async def test_resolves_only_existing_projection_without_binding_or_operation_state():
    first, second, selection, principal = setup()
    reader = SimpleNamespace(get=AsyncMock(side_effect=[first, second]))
    result = await SharedDeviceSessions(reader, None).execute(selection, context=principal)
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
        principal = replace(
            principal, actor=principal.actor.model_copy(update={"granted_scopes": ("device.read",)})
        )
    if case == "foreign":
        second = replace(second, owner_id="another")
    if case == "missing":
        second = None
    if case == "revoked":
        second = replace(second, lifecycle_state=DeviceLifecycleState.REVOKED)
    if case == "generation":
        second = replace(second, trust_epoch=second.trust_epoch + 1)
    reader = SimpleNamespace(get=AsyncMock(side_effect=[first, second]))
    with pytest.raises((PermissionError, AdmissionProblem)):
        await SharedDeviceSessions(reader, None).execute(selection, context=principal)
    if case == "unscoped":
        reader.get.assert_not_called()
