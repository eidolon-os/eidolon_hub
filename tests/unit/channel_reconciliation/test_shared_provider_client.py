import json

import httpx
import pytest
from eidolon_sdk.biz.control.shared_session import SharedSessionSelection
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id
from test_channel_reconciliation import REF

from hub.channel_reconciliation.domain import ChannelProviderError
from hub.channel_reconciliation.provider_client import ChannelProviderHttpClient


@pytest.mark.parametrize("wrong", [False, True])
async def test_shared_commands_use_existing_authenticated_http_and_validate_identity(wrong):
    second = REF.model_copy(
        update={"device_instance_id": named_device_instance_id("second-shared")}
    )
    selection = SharedSessionSelection(
        session_id="s1", devices=(REF, second), input_device_id=REF.device_instance_id
    )
    seen = []

    def handle(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer " + "x" * 32
        assert request.extensions["timeout"]["read"] == 30
        body = json.loads(request.content)
        assert body["owner_id"] == "owner"
        if request.url.path.endswith("/open"):
            return httpx.Response(
                200,
                json={
                    "session_id": "wrong" if wrong else "s1",
                    "state": "transport_ready",
                    "device_ids": [r.device_instance_id for r in selection.devices],
                },
            )
        return httpx.Response(200, json={"session_id": "s1", "state": "closed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = ChannelProviderHttpClient(http, token="x" * 32)
        if wrong:
            with pytest.raises(ChannelProviderError, match="invalid shared admission"):
                await client.open_shared_session(
                    selection=selection, owner_id="owner", specifications=[]
                )
        else:
            assert (
                await client.open_shared_session(
                    selection=selection, owner_id="owner", specifications=[]
                )
            )["state"] == "transport_ready"
            assert (await client.close_shared_session(session_id="s1", owner_id="owner"))[
                "state"
            ] == "closed"
            assert len(seen) == 2
