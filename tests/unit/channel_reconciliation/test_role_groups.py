from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from eidolon_sdk.biz.control.coordination import CoordinationSelection, CoordinationMember
from hub.admission.domain import AdmissionProblem
from hub.channel_reconciliation.role_groups import RoleGroups
from hub.channel_reconciliation.domain import ChannelProviderError
from test_shared_selection import setup

@pytest.mark.parametrize('case', ['valid','foreign','unscoped','refresh_failed'])
async def test_team_uses_current_lifecycle_and_existing_grant_refresh(case):
    first, second, _, context = setup()
    context=replace(context, actor=context.actor.model_copy(update={'granted_scopes':
        ('device.read',) if case=='unscoped' else ('device.conversation.control',)}))
    selection=CoordinationSelection(scenario='ip_role_group',session_id='team',input_device=first.device_ref,
        members=(CoordinationMember(companion_id='one',output_device=second.device_ref),))
    if case=='foreign': second=replace(second,owner_id='another')
    devices=SimpleNamespace(get=AsyncMock(side_effect=[first,second]))
    binding=SimpleNamespace(execute=AsyncMock(return_value=case!='refresh_failed'))
    provider=SimpleNamespace(role_group=AsyncMock(return_value={'state':'preparing'}))
    service=RoleGroups(devices,provider,channel_binding=binding)
    if case!='valid':
        with pytest.raises((AdmissionProblem,PermissionError,ChannelProviderError)):
            await service.open(selection,context=context)
        provider.role_group.assert_not_called()
        if case!='refresh_failed': binding.execute.assert_not_called()
    else:
        assert await service.open(selection,context=context)=={'state':'preparing'}
        assert binding.execute.await_count==2
        provider.role_group.assert_awaited_once_with(action='open',owner_id='owner_01',selection=selection,session_id='team')

@pytest.mark.parametrize('wrong', [False, True])
async def test_provider_team_payload_uses_explicit_order_and_checks_scene(wrong):
    import json
    import httpx
    from hub.channel_reconciliation.provider_client import ChannelProviderHttpClient
    first, second, _, _ = setup()
    selected=CoordinationSelection(scenario='ip_role_group',session_id='team',input_device=first.device_ref,
        members=(CoordinationMember(companion_id='one',output_device=second.device_ref),))
    def handle(request):
        assert request.url.path.endswith('/role-groups/open')
        assert request.headers['Authorization']=='Bearer '+'x'*32
        body=json.loads(request.content)
        assert body['owner_id']=='owner_01' and body['mock_order']==['one']
        assert body['selection']==selected.model_dump(mode='json')
        return httpx.Response(200,json=dict(session_id='wrong' if wrong else 'team',state='preparing',
            scenario='ip_role_group',completion_basis='native_playout',error=''))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client=ChannelProviderHttpClient(http,token='x'*32)
        if wrong:
            with pytest.raises(ChannelProviderError):
                await client.role_group(action='open',owner_id='owner_01',session_id='team',selection=selected)
        else:
            assert (await client.role_group(action='open',owner_id='owner_01',session_id='team',selection=selected))['state']=='preparing'
