"""An unknown outcome is settled by the observation that shows the effect."""

import asyncio

from eidolon_sdk.biz.smarthome import Command

from hub.integration.ledger import SqliteReceiptLedger
from hub.integration.observation import SqliteObservationCache
from hub.integration.store import IntegrationStore
from hub.smarthome.effects import satisfied
from hub.smarthome.runtime import SmartHomeRuntime

from .helpers import OWNER, Clock, FakeRegistry, cmd, home, request


class SlowCover:
    """Acts after the caller stopped waiting, like a cover that takes seconds to travel."""

    pushes_observations = True

    def __init__(self):
        self.acted = asyncio.Event()

    async def reconcile(self, owner, devices):
        pass

    async def states(self, owner, devices):
        return {}

    async def execute(self, owner, device, command):
        await asyncio.sleep(0.2)
        self.acted.set()
        return {"position": 100}


async def test_unknown_receipt_is_reconciled_by_a_later_observation(tmp_path):
    store = IntegrationStore(tmp_path / "integration.sqlite3")
    store.initialize()
    clock = Clock()
    registry = FakeRegistry({OWNER: home()})
    runtime = SmartHomeRuntime(
        registry=registry,
        providers={"virtual": SlowCover()},
        now_ms=clock,
        ledger=SqliteReceiptLedger(store),
        observations=SqliteObservationCache(store),
    )
    result = await runtime.execute(
        OWNER, request("slow", cmd("living.curtain", "position", "open"), deadline_ms=clock() + 50)
    )
    assert result.results[0].status == "unknown"
    receipt = await runtime.ledger.get(OWNER, "", "slow")
    assert (
        receipt.result["results"][0]["status"] == "unknown"
        and receipt.timestamps.reconciled_at_ms is None
    )

    # A partial position does not settle it; the final one does.
    clock.now_ms += 1_000
    await runtime.observe(OWNER, "living.curtain", reachable=True, state={"position": 60})
    assert (await runtime.ledger.get(OWNER, "", "slow")).result["results"][0]["status"] == "unknown"
    await runtime.observe(OWNER, "living.curtain", reachable=True, state={"position": 100})
    settled = await runtime.ledger.get(OWNER, "", "slow")
    assert settled.result["results"][0] == {
        "device_id": "living.curtain",
        "status": "succeeded",
        "code": None,
        "state": {"position": 100},
        "platform_answer": None,
    }
    assert settled.timestamps.reconciled_at_ms == clock()
    # Reconciliation is one-shot: a later observation does not rewrite it.
    await runtime.observe(OWNER, "living.curtain", reachable=True, state={"position": 0})
    assert (await runtime.ledger.get(OWNER, "", "slow")).result["results"][0]["state"] == {
        "position": 100
    }
    # The repeat of the same request still answers with the recorded (now settled) receipt.
    again = await runtime.execute(OWNER, request("slow", cmd("living.curtain", "position", "open")))
    assert again.results[0].status == "succeeded"


async def test_pending_unknown_expires(tmp_path):
    store = IntegrationStore(tmp_path / "integration.sqlite3")
    store.initialize()
    clock = Clock()
    runtime = SmartHomeRuntime(
        registry=FakeRegistry({OWNER: home()}),
        providers={"virtual": SlowCover()},
        now_ms=clock,
        ledger=SqliteReceiptLedger(store),
        observations=SqliteObservationCache(store),
    )
    await runtime.execute(
        OWNER, request("late", cmd("living.curtain", "position", "open"), deadline_ms=clock() + 50)
    )
    clock.now_ms += 11 * 60_000
    await runtime.observe(OWNER, "living.curtain", reachable=True, state={"position": 100})
    assert (await runtime.ledger.get(OWNER, "", "late")).result["results"][0]["status"] == "unknown"


def test_effects_cover_every_command():
    assert satisfied(
        Command(device_id="d", trait="on_off", command="toggle"), {"on": False}, {"on": True}
    )
    assert not satisfied(
        Command(device_id="d", trait="on_off", command="toggle"), {"on": True}, {"on": True}
    )
    assert satisfied(
        Command(device_id="d", trait="level", command="step", params={"delta": 10}),
        {"level": 50, "on": True},
        {"level": 60, "on": True},
    )
    assert satisfied(
        Command(device_id="d", trait="thermostat", command="set_target", params={"celsius": 24.5}),
        None,
        {"target_c": 24.5},
    )
    assert satisfied(
        Command(device_id="d", trait="operational", command="dock"), None, {"run_state": "docked"}
    )
    assert not satisfied(
        Command(device_id="d", trait="lock", command="lock"), None, {"locked": False}
    )
