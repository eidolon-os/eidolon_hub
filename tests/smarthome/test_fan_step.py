import pytest
from eidolon_sdk.biz.smarthome import Command
from eidolon_sdk.biz.smarthome.samples import apartment
from hub.smarthome.providers.virtual import VirtualProvider, apply_command
from hub.smarthome.effects import satisfied


@pytest.mark.parametrize(
    "start,delta,expected", [(0, -10, 0), (10, -10, 0), (30, 10, 40), (90, 20, 100), (100, 10, 100)]
)
def test_fan_step_boundaries_and_exact_receipt(start, delta, expected):
    c = Command(
        device_id="master.humidifier", trait="fan_speed", command="step", params={"delta": delta}
    )
    before = {"on": True, "speed": start}
    after = apply_command("fan", before, c)
    assert after == {"on": True, "speed": expected}
    assert satisfied(c, before, after)
    assert not satisfied(c, {}, after)
    assert not satisfied(c, before, {"speed": (expected + 1) % 101})


async def test_fan_steps_read_persisted_state_and_external_changes(tmp_path):
    p = VirtualProvider(tmp_path / "fan.sqlite3")
    p.initialize()
    device = apartment().device("master.humidifier")
    await p.reconcile("owner", [device])

    def cmd(verb, **params):
        return Command(device_id=device.device_id, trait="fan_speed", command=verb, params=params)

    assert (await p.execute("owner", device, cmd("step", delta=10)))["speed"] == 40
    assert (await p.execute("owner", device, cmd("step", delta=10)))["speed"] == 50
    await p.execute("owner", device, cmd("set", value=80))
    assert (await p.execute("owner", device, cmd("step", delta=-10)))["speed"] == 70
    restarted = VirtualProvider(tmp_path / "fan.sqlite3")
    assert (await restarted.execute("owner", device, cmd("step", delta=10)))["speed"] == 80
