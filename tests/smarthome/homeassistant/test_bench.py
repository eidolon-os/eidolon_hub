"""Against the Home Assistant Core bench (deploy/dev/homeassistant in eidolon_ops).

Skipped unless the bench token exists. The bench runs the demo integration, so
every mapped type has a fake device that reacts to services immediately.
"""

import os
from pathlib import Path

import aiohttp
import pytest
from eidolon_sdk.biz.smarthome import Command, Device

from hub.integration.accounts import ProviderAccountStore
from hub.integration.store import IntegrationStore
from hub.integration.vault import CredentialVault, generate_vault_key, load_vault_key
from hub.smarthome.ports import BindError
from hub.smarthome.providers.homeassistant.provider import HomeAssistantProvider

TOKEN_PATH = Path(
    os.environ.get(
        "EIDOLON_HA_BENCH_TOKEN_FILE",
        "~/ai/eidolon/.eidolon/mac-product/homeassistant/config/ha.token",
    )
).expanduser()
URL = os.environ.get("EIDOLON_HA_BENCH_URL", "http://127.0.0.1:8123")
OWNER, ACCOUNT = "owner-a", "acc_ha"

pytestmark = pytest.mark.skipif(not TOKEN_PATH.exists(), reason="no Home Assistant bench token")


@pytest.fixture
async def provider(tmp_path):
    store = IntegrationStore(tmp_path / "integration.sqlite3")
    store.initialize()
    vault = CredentialVault(store, load_vault_key({"EIDOLON_HUB_VAULT_KEY": generate_vault_key()}))
    async with aiohttp.ClientSession() as session:
        value = HomeAssistantProvider(
            session=session, vault=vault, accounts=ProviderAccountStore(store)
        )
        yield value
        for account in list(value._connections):
            await value.unbind(OWNER, account)


async def bind(provider):
    account = await provider.bind(
        OWNER, ACCOUNT, {"url": URL, "token": TOKEN_PATH.read_text().strip()}
    )
    assert account.status == "connected"
    connection = provider._connections[ACCOUNT]
    demo = [
        eid
        for eid in connection.states
        if eid.split(".")[0]
        in (
            "light",
            "cover",
            "climate",
            "fan",
            "lock",
            "media_player",
            "vacuum",
            "water_heater",
            "switch",
            "sensor",
        )
    ]
    await connection.expose(demo)
    return connection


async def test_bind_refuses_a_bad_token(provider):
    with pytest.raises(BindError) as refused:
        await provider.bind(OWNER, ACCOUNT, {"url": URL, "token": "nope"})
    assert refused.value.code == "UNAUTHORIZED"


async def test_discover_covers_every_mapped_type(provider):
    await bind(provider)
    found = await provider.discover(OWNER, ACCOUNT)
    kinds = {d.suggested_type for d in found}
    assert {
        "light",
        "cover",
        "climate",
        "fan",
        "lock",
        "media",
        "appliance",
        "water_heater",
    } <= kinds
    for d in found:
        Device(
            device_id="x",
            name="x",
            type=d.suggested_type,
            traits=d.traits,
            limits=d.limits,
            provider="homeassistant:a",
            provider_ref=d.external_ref,
        )


async def test_execute_is_confirmed_by_the_entity_state(provider):
    await bind(provider)
    found = {d.external_ref: d for d in await provider.discover(OWNER, ACCOUNT)}
    light_ref = next(
        r
        for r, d in found.items()
        if d.suggested_type == "light" and d.traits == ("on_off", "level")
    )
    light = Device(
        device_id="l",
        name="灯",
        type="light",
        traits=found[light_ref].traits,
        provider=f"homeassistant:{ACCOUNT}",
        provider_ref=light_ref,
    )
    off = await provider.execute(
        OWNER, light, Command(device_id="l", trait="on_off", command="off")
    )
    assert off["on"] is False
    dim = await provider.execute(
        OWNER, light, Command(device_id="l", trait="level", command="set", params={"value": 40})
    )
    assert dim["on"] is True and abs(dim["level"] - 40) <= 1
    connection = provider._connections[ACCOUNT]
    cover_ref = next(
        r
        for r, d in found.items()
        if d.suggested_type == "cover"
        and int(connection.states[r]["attributes"].get("supported_features") or 0) & 4
    )
    cover = Device(
        device_id="c",
        name="帘",
        type="cover",
        traits=("position",),
        provider=f"homeassistant:{ACCOUNT}",
        provider_ref=cover_ref,
    )
    opened = await provider.execute(
        OWNER, cover, Command(device_id="c", trait="position", command="set", params={"value": 70})
    )
    assert opened["position"] == 70
    ac_ref = next(r for r, d in found.items() if d.suggested_type == "climate")
    ac = Device(
        device_id="a",
        name="空调",
        type="climate",
        traits=("on_off", "thermostat"),
        limits=found[ac_ref].limits,
        provider=f"homeassistant:{ACCOUNT}",
        provider_ref=ac_ref,
    )
    low, high = ac.limits.target_c
    target = min(max(21, low), high)
    warm = await provider.execute(
        OWNER,
        ac,
        Command(
            device_id="a", trait="thermostat", command="set_target", params={"celsius": target}
        ),
    )
    assert warm["target_c"] == target


async def test_states_project_the_cache_without_io(provider):
    from eidolon_sdk.biz.smarthome import validate_device_state

    await bind(provider)
    found = {d.external_ref: d for d in await provider.discover(OWNER, ACCOUNT)}
    devices = [
        Device(
            device_id=f"d{i}",
            name="x",
            type=d.suggested_type,
            traits=d.traits,
            limits=d.limits,
            provider=f"homeassistant:{ACCOUNT}",
            provider_ref=d.external_ref,
        )
        for i, d in enumerate(found.values())
    ]
    states = await provider.states(OWNER, devices)
    assert len(states) >= len(devices) - 3  # unavailable demo entities are simply absent
    for device in devices:
        if device.device_id in states:
            validate_device_state(device, states[device.device_id])
