"""Execution invariants stay with Hub, using its real virtual Provider."""

import asyncio

import pytest
from eidolon_sdk.biz.smarthome import Device, Origin, Registry, initial_state

from hub.smarthome.providers.virtual import VirtualProvider
from hub.smarthome.runtime import IdempotencyConflict, SmartHomeRuntime

from .helpers import OTHER_OWNER, OWNER, Clock, FakeRegistry, cmd, home, request, scene


@pytest.fixture
def setup(tmp_path):
    provider = VirtualProvider(tmp_path / "virtual.sqlite3")
    provider.initialize()
    clock = Clock()
    registry = FakeRegistry({OWNER: home(), OTHER_OWNER: Registry(revision=1)})
    runtime = SmartHomeRuntime(registry=registry, providers={"virtual": provider}, now_ms=clock)
    return runtime, provider, registry, clock


async def test_concurrent_repeat_and_conflict(setup):
    runtime, _, _, _ = setup
    command = request("one", cmd("living.main_light", "level", "step", delta=10))
    results = await asyncio.gather(*(runtime.execute(OWNER, command) for _ in range(4)))
    assert all(x == results[0] for x in results)
    state = (await runtime.snapshot(OWNER))["status"]["living.main_light"]["state"]
    assert state["level"] == min(initial_state("light")["level"] + 10, 100)
    with pytest.raises(IdempotencyConflict):
        await runtime.execute(OWNER, request("one", cmd("living.main_light", "on_off", "off")))


async def test_touch_scope_and_owner_isolation(setup):
    runtime, _, _, _ = setup
    for panel in ("panel-a", "panel-b"):
        value = request("same", cmd("living.main_light", "on_off", "toggle")).model_copy(
            update={"origin": Origin(kind="touch", device_ref=panel)}
        )
        assert (await runtime.execute(OWNER, value)).results[0].status == "succeeded"
    assert (
        await runtime.execute(OTHER_OWNER, request("one", cmd("living.main_light", "on_off", "on")))
    ).results[0].status == "failed"
    assert not (await runtime.snapshot(OTHER_OWNER))["status"]


async def test_deadline_and_partial_results(setup):
    runtime, _, _, clock = setup
    assert (
        await runtime.execute(
            OWNER, request("late", cmd("living.ac", "on_off", "on"), deadline_ms=clock())
        )
    ).error == "DEADLINE_EXCEEDED"
    result = await runtime.execute(
        OWNER, request("partial", cmd("living.ac", "on_off", "on"), cmd("missing", "on_off", "on"))
    )
    assert [x.status for x in result.results] == ["succeeded", "failed"]


async def test_timeout_is_unknown_and_not_retried(setup):
    runtime, _, registry, clock = setup

    class Slow:
        calls = 0

        async def reconcile(self, owner, devices):
            pass

        async def states(self, owner, devices):
            return {}

        async def execute(self, owner, device, command):
            self.calls += 1
            await asyncio.sleep(1)

    provider = Slow()
    runtime = SmartHomeRuntime(registry=registry, providers={"virtual": provider}, now_ms=clock)
    value = request("timeout", cmd("living.ac", "on_off", "on"), deadline_ms=clock() + 10)
    result = await runtime.execute(OWNER, value)
    assert result.results[0].status == "unknown"
    assert await runtime.execute(OWNER, value) == result
    assert provider.calls == 1


async def test_scene_and_unknown_scene(setup):
    runtime, _, registry, _ = setup
    item = registry.registries[OWNER].scenes[0]
    result = await runtime.execute(OWNER, scene("scene", item.scene_id))
    assert len(result.results) == len(item.actions)
    assert all(x.status == "succeeded" for x in result.results)
    assert (await runtime.execute(OWNER, scene("missing", "missing"))).error == "UNKNOWN_SCENE"


async def test_state_refresh_independent_of_registry_revision(setup):
    runtime, provider, registry, _ = setup
    await runtime.snapshot(OWNER)
    device = registry.registries[OWNER].device("living.main_light")
    await provider.execute(OWNER, device, cmd(device.device_id, "on_off", "on"))
    assert (await runtime.snapshot(OWNER))["status"][device.device_id]["state"]["on"] is True


async def test_unregistered_provider_and_registry_removal(setup):
    runtime, _, registry, _ = setup
    registry.registries[OWNER] = Registry(
        revision=2,
        devices=(Device(device_id="cloud", name="cloud", type="light", provider="external"),),
    )
    assert not (await runtime.snapshot(OWNER))["status"]["cloud"]["online"]
    result = await runtime.execute(OWNER, request("cloud", cmd("cloud", "on_off", "on")))
    assert result.results[0].status == "failed"


async def test_registry_failure_never_executes(setup):
    runtime, provider, registry, _ = setup
    del registry.registries[OWNER]
    with pytest.raises(KeyError):
        await runtime.execute(OWNER, request("one", cmd("living.ac", "on_off", "on")))
    registry.registries[OWNER] = home()
    assert (await runtime.execute(OWNER, request("one", cmd("living.ac", "on_off", "on")))).results[
        0
    ].status == "succeeded"


async def test_http_authentication_and_conflict(setup):
    import httpx
    from fastapi import FastAPI

    from hub.smarthome.http import create_smarthome_router

    runtime, *_ = setup
    app = FastAPI()
    app.include_router(create_smarthome_router(lambda: runtime, lambda: "internal-secret"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://hub"
    ) as client:
        assert (
            await client.post("/api/smarthome/v1/snapshot", json={"owner_id": OWNER})
        ).status_code == 401
        headers = {"Authorization": "Bearer internal-secret"}
        assert (
            await client.post(
                "/api/smarthome/v1/snapshot", json={"owner_id": OWNER}, headers=headers
            )
        ).status_code == 200
        for action, status in [("on", 200), ("off", 409)]:
            response = await client.post(
                "/api/smarthome/v1/execute",
                json={
                    "owner_id": OWNER,
                    "request": request("same", cmd("living.ac", "on_off", action)).model_dump(
                        mode="json"
                    ),
                },
                headers=headers,
            )
            assert response.status_code == status


async def test_legacy_state_is_not_silently_replaced(tmp_path):
    import httpx

    from hub.composition.smarthome import build_smarthome
    from hub.config import HubConfig, PersistenceConfig, SmartHomeConfig

    old = tmp_path / "channel/smarthome.sqlite3"
    old.parent.mkdir()
    old.write_bytes(b"existing-authority")
    config = HubConfig(
        persistence=PersistenceConfig(path=str(tmp_path / "hub/hub.sqlite3")),
        smarthome=SmartHomeConfig(workspace_url="http://data"),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="offline migration"):
            build_smarthome(
                config,
                client,
                {
                    "EIDOLON_HUB_SMARTHOME_TOKEN": "s" * 32,
                    "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN": "w" * 32,
                },
            )
    assert not (tmp_path / "hub/smarthome.sqlite3").exists()
    assert old.read_bytes() == b"existing-authority"


async def test_repeated_fan_delta_is_idempotent_per_request(setup):
    runtime, _, _, _ = setup
    command=request('fan-once',cmd('master.humidifier','fan_speed','step',delta=10))
    values=await asyncio.gather(*(runtime.execute(OWNER,command) for _ in range(4)))
    assert all(v==values[0] for v in values)
    assert (await runtime.snapshot(OWNER))['status']['master.humidifier']['state']['speed']==40
    await runtime.execute(OWNER,request('fan-next',cmd('master.humidifier','fan_speed','step',delta=10)))
    assert (await runtime.snapshot(OWNER))['status']['master.humidifier']['state']['speed']==50
