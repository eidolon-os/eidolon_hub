from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI
from test_shared_selection import setup

from hub.admission.auth import JwtAdmissionActorProvider
from hub.channel_reconciliation.shared_selection import SharedDeviceSessions
from hub.device_control.shared_session_http import create_shared_session_router

SECRET = b"shared-route-test-signing-secret-32-bytes"


def token(scopes, owner="owner_01"):
    return jwt.encode(
        {
            "sub": "eidolon-admin/admission-consumer",
            "presenter": "eidolon-admin/admission-consumer",
            "aud": "eidolon-admission",
            "actor": {
                "principal_id": "controller_01",
                "owner_domain_id": "owner-domain_01",
                "granted_scopes": scopes,
                "authentication_strength": "software",
            },
            "owner_domain_id": "owner-domain_01",
            "business_owner_id": owner,
            "scopes": scopes,
            "exp": 4_000_000_000,
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.mark.parametrize("case", ["ok", "missing", "read_only", "foreign", "body_owner"])
async def test_real_jwt_authority_and_projection_reach_existing_provider_client(case):
    first, second, selection, _ = setup()
    reader = SimpleNamespace(get=AsyncMock(side_effect=[first, second]))
    provider = SimpleNamespace(
        open_shared_session=AsyncMock(return_value={"state": "transport_ready"}),
        close_shared_session=AsyncMock(return_value={"state": "closed"}),
    )
    service = SharedDeviceSessions(reader, provider)
    app = FastAPI()
    app.include_router(
        create_shared_session_router(
            service=lambda: service, actor_provider=JwtAdmissionActorProvider(secret=SECRET)
        )
    )
    scopes = ["device.read"] if case == "read_only" else ["device.shared-session.control"]
    headers = (
        {}
        if case == "missing"
        else {
            "Authorization": "Bearer " + token(scopes, "owner_other" if case == "foreign" else "owner_01")
        }
    )
    body = selection.model_dump(mode="json")
    if case == "body_owner":
        body["owner_id"] = "owner_01"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/device-control/v1/shared-sessions/open", json=body, headers=headers
        )
        assert (
            response.status_code
            == {"ok": 200, "missing": 401, "read_only": 403, "foreign": 403, "body_owner": 422}[
                case
            ]
        )
        if case == "ok":
            assert provider.open_shared_session.call_args.kwargs["owner_id"] == "owner_01"
            assert len(provider.open_shared_session.call_args.kwargs["specifications"]) == 2
            response = await client.post(
                "/api/device-control/v1/shared-sessions/close",
                json={"session_id": selection.session_id},
                headers=headers,
            )
            assert response.status_code == 200
            provider.close_shared_session.assert_awaited_once_with(
                session_id=selection.session_id, owner_id="owner_01"
            )
        else:
            provider.open_shared_session.assert_not_called()
