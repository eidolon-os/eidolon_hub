from __future__ import annotations

import pytest

from hub.adapters.security.device_registry_reader import DeviceRegistryReaderAuthorizer
from hub.ports.identity import ManagementPermission

TOKEN = "device-registry-reader-token-000001"


def _authorizer() -> DeviceRegistryReaderAuthorizer:
    return DeviceRegistryReaderAuthorizer(device_registry_reader_token=TOKEN)


async def test_kernel_may_read_one_device_it_names() -> None:
    principal = await _authorizer().authorize(
        credential=f"Bearer {TOKEN}",
        permission=ManagementPermission.DEVICE_GET,
        owner_scope="owner-domain_01",
        device_id="device_01",
    )

    assert principal.subject_id == "eidolon-kernel/device-authority"
    assert principal.roles == frozenset({"device-registry-reader"})


async def test_kernel_may_follow_the_claim_stream() -> None:
    principal = await _authorizer().authorize(
        credential=f"Bearer {TOKEN}",
        permission=ManagementPermission.CLAIM_EVENTS,
        owner_scope=None,
        device_id=None,
    )

    assert principal.roles == frozenset({"device-registry-reader"})


async def test_a_read_that_names_no_device_is_not_an_exact_read() -> None:
    """The reader is exact by construction, not by the caller's good manners."""

    for owner_scope, device_id in ((None, "device_01"), ("owner-domain_01", None)):
        with pytest.raises(PermissionError, match="exact reads"):
            await _authorizer().authorize(
                credential=f"Bearer {TOKEN}",
                permission=ManagementPermission.DEVICE_GET,
                owner_scope=owner_scope,
                device_id=device_id,
            )


@pytest.mark.parametrize(
    "credential",
    ["", "Bearer", "Basic abc", f"Bearer {TOKEN}x", "Bearer wrong-token-000000000000001"],
)
async def test_anything_that_is_not_this_token_is_refused(credential: str) -> None:
    with pytest.raises(PermissionError):
        await _authorizer().authorize(
            credential=credential,
            permission=ManagementPermission.DEVICE_GET,
            owner_scope="owner-domain_01",
            device_id="device_01",
        )


def test_a_token_too_short_to_be_a_secret_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        DeviceRegistryReaderAuthorizer(device_registry_reader_token="short")
