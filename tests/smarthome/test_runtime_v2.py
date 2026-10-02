"""Runtime guarantees added for real Providers: durable receipts, lock-out I/O, delegated."""

import asyncio

import pytest
from eidolon_sdk.biz.smarthome import Device, Registry

from hub.integration.ledger import SqliteReceiptLedger
from hub.integration.observation import SqliteObservationCache
from hub.integration.store import IntegrationStore
from hub.smarthome.ports import Delegated
from hub.smarthome.providers.virtual import VirtualProvider
from hub.smarthome.runtime import SmartHomeRuntime

from .helpers import OWNER, Clock, FakeRegistry, cmd, home, request


class Spoken:
    """A delegated platform: says it did it, reports no state."""

    pushes_observations = True

    def __init__(self):
        self.said = []

    async def reconcile(self, owner, devices):
        pass

    async def states(self, owner, devices):
        return {}

    async def execute(self, owner, device, command):
        self.said.append((device.name, command.command))
        return Delegated(f"好的，为您{'打开' if command.command == 'on' else '关闭'}{device.name}")


def home_with_spoken() -> Registry:
    registry = home().model_dump(mode="json")
    registry["devices"].append(
        {
            "device_id": "zb.lamp",
            "name": "主卧吸顶灯",
            "type": "light",
            "area_id": "master",
            "provider": "zhoubian:acc1",
            "provider_ref": "ATARS.property.power1",
            "traits": ["on_off"],
            "source": "imported",
        }
    )
    return Registry.model_validate(registry)


@pytest.fixture
def setup(tmp_path):
    store = IntegrationStore(tmp_path / "integration.sqlite3")
    store.initialize()
    virtual = VirtualProvider(tmp_path / "virtual.sqlite3")
    virtual.initialize()
    spoken = Spoken()
    clock = Clock()
    registry = FakeRegistry({OWNER: home_with_spoken()})

    def make():
        return SmartHomeRuntime(
            registry=registry,
            providers={"virtual": virtual, "zhoubian": spoken},
            now_ms=clock,
            ledger=SqliteReceiptLedger(IntegrationStore(store.path)),
            observations=SqliteObservationCache(IntegrationStore(store.path)),
        )

    return make, spoken, clock


async def test_delegated_is_never_succeeded_and_has_no_state(setup):
    make, spoken, _ = setup
    runtime = make()
    result = await runtime.execute(OWNER, request("say", cmd("zb.lamp", "on_off", "on")))
    item = result.results[0]
    assert (
        item.status == "delegated"
        and item.platform_answer == "好的，为您打开主卧吸顶灯"
        and item.state is None
    )
    assert spoken.said == [("主卧吸顶灯", "on")]
    status = (await runtime.snapshot(OWNER))["status"]["zb.lamp"]
    assert status == {"online": True, "state": {}}
    # A trait the device does not declare is refused before the platform hears of it.
    refused = await runtime.execute(OWNER, request("dim", cmd("zb.lamp", "level", "set", value=10)))
    assert (
        refused.results[0].status == "failed" and refused.results[0].code == "UNSUPPORTED_COMMAND"
    )
    assert len(spoken.said) == 1


async def test_receipts_survive_a_restart(setup):
    make, spoken, _ = setup
    first = await make().execute(OWNER, request("once", cmd("living.main_light", "on_off", "on")))
    again = await make().execute(OWNER, request("once", cmd("living.main_light", "on_off", "on")))
    assert again == first
    receipt = await make().ledger.get(OWNER, "", "once")
    assert receipt.result["results"][0]["status"] == "succeeded"
    assert receipt.timestamps.provider_started_at_ms is not None
    assert receipt.timestamps.completed_at_ms is not None


async def test_slow_device_does_not_block_another_owner_request(setup):
    make, spoken, clock = setup
    runtime = make()
    gate = asyncio.Event()

    class Stuck:
        pushes_observations = True

        async def reconcile(self, owner, devices):
            pass

        async def states(self, owner, devices):
            return {}

        async def execute(self, owner, device, command):
            await gate.wait()
            return Delegated("done")

    runtime._providers["zhoubian"] = Stuck()
    slow = asyncio.create_task(
        runtime.execute(
            OWNER, request("slow", cmd("zb.lamp", "on_off", "on"), deadline_ms=clock() + 60_000)
        )
    )
    await asyncio.sleep(0.05)
    quick = await asyncio.wait_for(
        runtime.execute(OWNER, request("quick", cmd("living.main_light", "on_off", "on"))), 1.0
    )
    assert quick.results[0].status == "succeeded"
    gate.set()
    assert (await slow).results[0].status == "delegated"


async def test_orphaned_device_is_offline(setup):
    make, _, _ = setup
    runtime = make()
    registry = home_with_spoken().model_dump(mode="json")
    registry["devices"][-1]["orphaned"] = True
    registry["revision"] = 2
    runtime._registry_source.registries[OWNER] = Registry.model_validate(registry)
    result = await runtime.execute(OWNER, request("gone", cmd("zb.lamp", "on_off", "on")))
    assert result.results[0].code == "DEVICE_OFFLINE"
    assert (await runtime.snapshot(OWNER))["status"]["zb.lamp"]["online"] is False


async def test_observed_changes_stream(setup):
    make, _, _ = setup
    runtime = make()
    await runtime.snapshot(OWNER)
    seq = await runtime.observations.latest_seq(OWNER)
    await runtime.observe(OWNER, "zb.lamp", reachable=False, state=None)
    changes = await runtime.observations.changes_since(OWNER, seq, 0.1)
    assert [(c.target, c.reachable) for c in changes] == [("zb.lamp", False)]
    assert isinstance(Device.model_validate(home_with_spoken().devices[-1].model_dump()), Device)
