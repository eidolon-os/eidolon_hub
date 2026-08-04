"""Workspace-level proof that Kernel consumes Hub's real exact-device response."""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from jose import jwt

from hub.composition.app import create_composed_app
from hub.config import (
    ChannelProviderConfig,
    DiscoveryConfig,
    HubConfig,
    MdnsDiscoveryConfig,
    OnboardingConfig,
    PersistenceConfig,
)

KERNEL_ROOT = Path(__file__).resolve().parents[3] / "eidolon_kernel"
if not (KERNEL_ROOT / "eidolon_kernel").is_dir():
    pytest.skip("eidolon_kernel sibling is required for the joint contract test", allow_module_level=True)
sys.path.insert(0, str(KERNEL_ROOT))

HubHttpDeviceAuthority = importlib.import_module(
    "eidolon_kernel.adapters.device_registry.hub_http"
).HubHttpDeviceAuthority
ContractRegistry = importlib.import_module(
    "eidolon_kernel.contracts.registry"
).ContractRegistry
SqliteMountStore = importlib.import_module(
    "eidolon_kernel.adapters.persistence.sqlite"
).SqliteMountStore
InMemoryMountProjection = importlib.import_module(
    "eidolon_kernel.adapters.projection.memory"
).InMemoryMountProjection
SystemClock = importlib.import_module("eidolon_kernel.adapters.runtime").SystemClock
ReconcileMountPrerequisites = importlib.import_module(
    "eidolon_kernel.application.device_mounts"
).ReconcileMountPrerequisites
DeviceMount = importlib.import_module("eidolon_kernel.domain.model").DeviceMount


class _UnexpectedCompanionAuthority:
    async def get_companion(self, *, companion_id: str):
        raise AssertionError("revoked Device must short-circuit Companion lookup")


def _token(*, secret: str, subject: str, role: str) -> str:
    return jwt.encode(
        {
            "sub": subject,
            "aud": "eidolon-hub",
            "roles": [role],
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        secret,
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_kernel_consumes_approval_and_reconciles_real_hub_revocation(
    tmp_path, monkeypatch, httpserver
) -> None:
    management_secret = "joint-contract-management-secret-0001"
    provider_token = "joint-contract-provider-secret-000001"
    monkeypatch.setenv("EIDOLON_HUB_MANAGEMENT_JWT_SECRET", management_secret)
    monkeypatch.setenv(
        "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
        provider_token,
    )
    registry_reader_token = "joint-contract-registry-reader-token-0001"
    monkeypatch.setenv(
        "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN",
        registry_reader_token,
    )
    admin_token = _token(
        secret=management_secret,
        subject="joint-contract/admin",
        role="hub-admin",
    )
    app = create_composed_app(
        HubConfig(
            onboarding=OnboardingConfig(public_base_url="https://hub.contract.invalid"),
            discovery=DiscoveryConfig(mdns=MdnsDiscoveryConfig(enabled=False)),
            channel_provider=ChannelProviderConfig(
                contract_url=httpserver.url_for("/v1")
            ),
            persistence=PersistenceConfig(path=str(tmp_path / "joint-contract.sqlite3")),
        )
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://hub.test",
        ) as hub_client:
            enrollment = await hub_client.post(
                "/api/device-onboarding/v1/enrollments",
                json={
                    "operation": "device.enrollment",
                    "request_id": "joint-enrollment-1",
                    "retrieval_token": "device-generated-random-token-000001",
                    "identity": {"device_id": "joint-device-1"},
                    "manifest": {
                        "schema_version": 1,
                        "title": "Joint Contract Device",
                    },
                    "display_name": "Joint Contract Device",
                    "device_kind": "reference-device",
                },
            )
            assert enrollment.status_code == 200, enrollment.text
            approval = await hub_client.post(
                "/api/device-management/v1/devices/joint-device-1/approval",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "operation": "device.approval",
                    "request_id": "joint-approval-1",
                    "owner_id": "joint-owner-1",
                },
            )
            assert approval.status_code == 200, approval.text

            authority = HubHttpDeviceAuthority(
                base_url="http://hub.test",
                bearer_token=registry_reader_token,
                contracts=ContractRegistry(),
                client=hub_client,
            )
            admission = await authority.get_device(
                owner_id="joint-owner-1",
                device_id="joint-device-1",
            )

            mount_store = SqliteMountStore(tmp_path / "joint-kernel.sqlite3")
            projection = InMemoryMountProjection()
            mounted = DeviceMount.first(
                device_id="joint-device-1",
                owner_id="joint-owner-1",
                companion_id="joint-companion-1",
                at=datetime.now(UTC),
                request_id="joint-mount-1",
                fingerprint="sha256:" + "a" * 64,
            )
            mount_store.commit(
                mount=mounted,
                expected_revision=0,
                operation="device.mount",
                event_type="eidolon.kernel.device-mounted.v1",
                event_data={"test_setup": True},
            )
            projection.rebuild(mount_store.list_all())

            httpserver.expect_request(
                "/v1/device-channels/revoke",
                method="POST",
                headers={"Authorization": f"Bearer {provider_token}"},
            ).respond_with_json(
                {
                    "operation": "channel.revoked-device",
                    "operation_id": "joint-revocation-1",
                    "device_id": "joint-device-1",
                }
            )
            revocation = await hub_client.post(
                "/api/device-management/v1/devices/joint-device-1/revocation",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "operation": "device.revocation",
                    "request_id": "joint-revocation-1",
                    "reason": "joint-contract-test",
                },
            )
            assert revocation.status_code == 200, revocation.text
            reconciliation = await ReconcileMountPrerequisites(
                mount_store,
                projection,
                authority,
                _UnexpectedCompanionAuthority(),
                SystemClock(),
            ).execute()
            reconciled_mount = mount_store.get("joint-device-1")
            mount_store.close()

    assert admission.device_id == "joint-device-1"
    assert admission.owner_id == "joint-owner-1"
    assert admission.status == "approved"
    assert admission.manifest_revision.startswith("sha256:")
    assert reconciliation.unmounted == 1
    assert reconciled_mount.active is False
    assert reconciled_mount.revision == 2
