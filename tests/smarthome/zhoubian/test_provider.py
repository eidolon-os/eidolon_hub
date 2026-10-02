"""The adapter against the in-process cloud mock: bind, discover, delegate, refuse."""

import httpx
import pytest
from eidolon_sdk.biz.smarthome import Command, Device, SmartHomeError

from hub.integration.accounts import ProviderAccountStore
from hub.integration.store import IntegrationStore
from hub.integration.vault import CredentialVault, generate_vault_key, load_vault_key
from hub.smarthome.ports import BindError, Delegated
from hub.smarthome.providers.zhoubian.cloud_mock import (
    DEFAULT_APP_ID,
    DEFAULT_APP_SECRET,
    DEFAULT_PHONE,
    create_app,
)
from hub.smarthome.providers.zhoubian.provider import ZhoubianProvider, external_ref, suggest_type

OWNER = "owner-a"
ACCOUNT = "acc_demo"
FIELDS = {
    "base_url": "http://cloud",
    "app_id": DEFAULT_APP_ID,
    "app_secret": DEFAULT_APP_SECRET,
    "phone": DEFAULT_PHONE,
}


@pytest.fixture
async def setup(tmp_path):
    store = IntegrationStore(tmp_path / "integration.sqlite3")
    store.initialize()
    vault = CredentialVault(store, load_vault_key({"EIDOLON_HUB_VAULT_KEY": generate_vault_key()}))
    accounts = ProviderAccountStore(store)
    cloud = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cloud), base_url="http://cloud"
    ) as client:
        provider = ZhoubianProvider(
            client=client,
            vault=vault,
            accounts=accounts,
            host_identity="host-test",
            poll_interval_s=0.01,
        )
        yield provider, cloud, vault, accounts


def device(name: str, ref: str, kind: str = "switch") -> Device:
    return Device(
        device_id="d." + ref[-6:],
        name=name,
        type=kind,
        traits=("on_off",),
        provider=f"zhoubian:{ACCOUNT}",
        provider_ref=ref,
        source="imported",
    )


async def test_bind_discover_and_delegated_control(setup):
    provider, cloud, vault, accounts = setup
    account = await provider.bind(OWNER, ACCOUNT, FIELDS)
    assert account.status == "connected" and account.label == "我的家"
    assert set(await vault.names(ACCOUNT)) >= {"app_secret", "user_secret", "phone", "home_id"}
    found = await provider.discover(OWNER, ACCOUNT)
    assert [(d.name, d.area_name, d.reachable) for d in found] == [
        ("主卧吸顶灯", "主卧", True),
        ("客厅筒灯", "客厅", True),
        ("主卧窗帘", "主卧", True),
        ("客厅空调", "客厅", False),
    ]
    assert found[0].external_ref == "ATARS1Bj0001D83BDA303CD8.property.power1"
    assert [(d.suggested_type, d.traits) for d in found] == [
        ("switch", ("on_off",)),
        ("switch", ("on_off",)),
        ("cover", ("position",)),
        ("climate", ("on_off",)),
    ]
    # Every discovered device must be a valid registry device, or the import would refuse it.
    for d in found:
        Device(
            device_id="x",
            name="x",
            type=d.suggested_type,
            traits=d.traits,
            provider="zhoubian:a",
            provider_ref=d.external_ref,
        )
    lamp = device("主卧吸顶灯", found[0].external_ref)
    outcome = await provider.execute(
        OWNER, lamp, Command(device_id=lamp.device_id, trait="on_off", command="on")
    )
    assert isinstance(outcome, Delegated) and outcome.answer == "好的，为您打开主卧吸顶灯"
    state = cloud.state.tenant.devices["hxxx01"][0]
    assert state.on is True
    # The sentence the platform saw is the structured command said back, not a user's words.
    assert cloud.state.calls[-1]["body"] == {"query": "打开主卧吸顶灯", "homeId": "hxxx01"}


async def test_refusals_map_to_codes(setup):
    provider, cloud, _, _ = setup
    await provider.bind(OWNER, ACCOUNT, FIELDS)
    ac = device("客厅空调", "ATARS1Bj0002AC00000001", "climate")
    with pytest.raises(SmartHomeError) as offline:
        await provider.execute(
            OWNER, ac, Command(device_id=ac.device_id, trait="on_off", command="on")
        )
    assert offline.value.code == "DEVICE_OFFLINE"
    ghost = device("不存在的灯", "nope")
    with pytest.raises(SmartHomeError) as missing:
        await provider.execute(
            OWNER, ghost, Command(device_id=ghost.device_id, trait="on_off", command="on")
        )
    assert missing.value.code == "UNKNOWN_DEVICE"
    lamp = device("主卧吸顶灯", "x", "light")
    with pytest.raises(SmartHomeError) as unsaid:
        await provider.execute(
            OWNER,
            lamp,
            Command(device_id=lamp.device_id, trait="level", command="set", params={"value": 10}),
        )
    assert unsaid.value.code == "PLATFORM_REJECTED"
    with pytest.raises(SmartHomeError):
        await provider.execute(
            "owner-b", lamp, Command(device_id=lamp.device_id, trait="on_off", command="on")
        )


async def test_bind_refuses_bad_credentials_and_restores_from_vault(setup):
    provider, cloud, vault, accounts = setup
    with pytest.raises(BindError):
        await provider.bind(OWNER, ACCOUNT, {**FIELDS, "app_secret": "wrong"})
    with pytest.raises(BindError) as missing:
        await provider.bind(OWNER, ACCOUNT, {"base_url": "http://cloud"})
    assert missing.value.code == "MISSING_FIELDS"
    await provider.bind(OWNER, ACCOUNT, FIELDS)
    # A new process: nothing in memory, everything in the vault and account store.
    fresh = ZhoubianProvider(
        client=provider._client, vault=vault, accounts=accounts, host_identity="host-test"
    )
    await fresh.restore()
    assert (await fresh.discover(OWNER, ACCOUNT))[1].name == "客厅筒灯"


async def test_observe_reports_reachability_changes(setup):
    provider, cloud, _, _ = setup
    await provider.bind(OWNER, ACCOUNT, FIELDS)
    await provider.discover(OWNER, ACCOUNT)
    stream = provider.observe(OWNER, ACCOUNT)
    cloud.state.tenant.devices["hxxx01"][1].connected = False
    change = await anext(stream)
    assert (
        change.device_id == "ATARS1Bj0001D83BDA303CD8.property.power2" and change.reachable is False
    )
    await stream.aclose()


async def test_token_refresh_on_expiry(setup):
    provider, cloud, _, _ = setup
    await provider.bind(OWNER, ACCOUNT, FIELDS)
    session = provider._sessions[ACCOUNT]
    old = session.access_token
    # Platform forgets the token: the adapter refreshes once and succeeds.
    cloud.state.tenant.tokens.pop(old)
    assert len(await provider.discover(OWNER, ACCOUNT)) == 4
    assert session.access_token != old


def test_helpers():
    assert external_ref("ATARS1Bj0001", "property.power1") == "ATARS1Bj0001.property.power1"
    assert external_ref("ATARS1Bj0001", None) == "ATARS1Bj0001"
    assert suggest_type("switch", "开关") == "switch"
    assert suggest_type("ac", "空调") == "climate"
    assert suggest_type("", "智能窗帘") == "cover"
    assert suggest_type("other", "未知") == "switch"
