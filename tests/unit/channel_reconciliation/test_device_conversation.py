from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from eidolon_sdk.biz.control.device_conversation import DeviceConversationSelection
from hub.admission.domain import AdmissionProblem
from hub.channel_reconciliation.device_conversation import DeviceConversations
from hub.domain.devices.entities import DeviceLifecycleState
from test_shared_selection import setup

@pytest.mark.parametrize('case', ['valid', 'unscoped', 'foreign', 'missing', 'revoked', 'generation'])
async def test_both_refs_are_authorized_before_any_transport_mutation(case):
    first, second, selection, principal = setup()
    principal = replace(principal, actor=principal.actor.model_copy(
        update={'granted_scopes': ('device.read',) if case == 'unscoped' else ('device.conversation.control',)}))
    directed = DeviceConversationSelection(session_id=selection.session_id,
        input_device=first.device_ref, output_device=second.device_ref, target_companion_id='selected')
    if case == 'foreign': second = replace(second, owner_id='another')
    if case == 'missing': second = None
    if case == 'revoked': second = replace(second, lifecycle_state=DeviceLifecycleState.REVOKED)
    if case == 'generation': second = replace(second, trust_epoch=second.trust_epoch + 1)
    reader = SimpleNamespace(get=AsyncMock(side_effect=[first, second]))
    provider = SimpleNamespace(device_conversation=AsyncMock(return_value={'state': 'preparing'}))
    binding = SimpleNamespace(execute=AsyncMock(return_value=True))
    service = DeviceConversations(reader, provider, channel_binding=binding)
    if case != 'valid':
        with pytest.raises((PermissionError, AdmissionProblem)):
            await service.open(directed, context=principal)
        binding.execute.assert_not_called()
        provider.device_conversation.assert_not_called()
    else:
        assert await service.open(directed, context=principal) == {'state': 'preparing'}
        assert binding.execute.await_count == 2
        provider.device_conversation.assert_awaited_once_with(action='open', owner_id='owner_01',
            selection=directed, session_id=directed.session_id)
