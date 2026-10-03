"""Entity → SDK mapping rules, on recorded Home Assistant entity shapes."""

import pytest
from eidolon_sdk.biz.smarthome import Command, Device, Limits, validate_device_state

from hub.smarthome.providers.homeassistant.mapping import (
    Entity,
    discover,
    project_state,
    satisfied,
    to_service_call,
)


def entity(entity_id, state, name="x", area=None, device_id=None, device_class=None, **attributes):
    return Entity(entity_id, state, attributes, name, area, device_id, device_class)


def device(kind, ref, traits=None, limits=None):
    return Device(
        device_id="d",
        name="x",
        type=kind,
        traits=traits,
        limits=limits,
        provider="homeassistant:a",
        provider_ref=ref,
    )


def test_discover_maps_domains_features_and_groups_sensors():
    found = discover(
        [
            entity(
                "light.ceiling",
                "on",
                "客厅主灯",
                "客厅",
                brightness=128,
                supported_color_modes=["brightness"],
            ),
            entity("light.plain", "off", "阳台灯", supported_color_modes=["onoff"]),
            entity("cover.curtain", "open", "窗帘", current_position=100, supported_features=15),
            entity(
                "climate.ac",
                "cool",
                "空调",
                temperature=26,
                min_temp=16,
                max_temp=30,
                hvac_modes=["off", "cool", "heat", "fan_only"],
            ),
            entity("fan.f", "on", "风扇", supported_features=1, percentage=50),
            entity("media_player.tv", "playing", "电视", supported_features=4, volume_level=0.3),
            entity("vacuum.v", "docked", "扫地机"),
            entity("lock.door", "locked", "大门"),
            entity(
                "sensor.t", "23.5", "卧室温度", "卧室", device_id="dev1", device_class="temperature"
            ),
            entity("sensor.h", "55", "卧室湿度", "卧室", device_id="dev1", device_class="humidity"),
            entity("sensor.power", "12", "功率", device_class="power"),
            entity("binary_sensor.motion", "off", "人感"),
            entity("switch.plug", "unavailable", "插座"),
        ]
    )
    by_ref = {d.external_ref: d for d in found}
    assert (
        by_ref["light.ceiling"].traits == ("on_off", "level")
        and by_ref["light.ceiling"].area_name == "客厅"
    )
    assert by_ref["light.plain"].traits == ("on_off",)
    assert by_ref["cover.curtain"].suggested_type == "cover" and by_ref["cover.curtain"].traits == (
        "position",
    )
    assert by_ref["climate.ac"].limits == Limits(target_c=(16, 30), modes=("cool", "heat", "fan"))
    assert by_ref["fan.f"].traits == ("on_off", "fan_speed")
    assert by_ref["media_player.tv"].traits == ("on_off", "volume")
    assert by_ref["vacuum.v"].suggested_type == "appliance"
    assert by_ref["lock.door"].suggested_type == "lock"
    assert (
        by_ref["sensor.h:sensor.t"].suggested_type == "sensor"
        and by_ref["sensor.h:sensor.t"].name == "卧室"
    )
    assert "sensor.power" not in by_ref and "binary_sensor.motion" not in by_ref
    assert by_ref["switch.plug"].reachable is False
    # Every discovered device is a valid registry device with a valid initial projection.
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


@pytest.mark.parametrize(
    ("kind", "ent", "traits", "expected"),
    [
        (
            "light",
            entity("light.l", "on", brightness=255),
            ("on_off", "level"),
            {"on": True, "level": 100},
        ),
        ("light", entity("light.l", "off"), ("on_off",), {"on": False}),
        (
            "climate",
            entity("climate.c", "heat", temperature=22.5, current_temperature=20),
            ("on_off", "thermostat"),
            {"on": True, "mode": "heat", "target_c": 22.5, "current_c": 20},
        ),
        (
            "climate",
            entity("climate.c", "off", temperature=24),
            ("on_off", "thermostat"),
            {"on": False, "mode": "cool", "target_c": 24, "current_c": None},
        ),
        ("cover", entity("cover.c", "closed", current_position=0), ("position",), {"position": 0}),
        ("cover", entity("cover.c", "open"), ("position",), {"position": 100}),
        (
            "fan",
            entity("fan.f", "on", percentage=66),
            ("on_off", "fan_speed"),
            {"on": True, "speed": 66},
        ),
        (
            "media",
            entity("media_player.m", "playing", volume_level=0.42, is_volume_muted=True),
            ("on_off", "volume"),
            {"on": True, "volume": 42, "muted": True},
        ),
        ("appliance", entity("vacuum.v", "cleaning"), ("operational",), {"run_state": "running"}),
        ("lock", entity("lock.l", "unlocked"), ("lock",), {"locked": False}),
        (
            "water_heater",
            entity("water_heater.w", "eco", temperature=45, current_temperature=40),
            ("on_off", "thermostat"),
            {"on": True, "mode": "heat", "target_c": 45, "current_c": 40},
        ),
    ],
)
def test_project_state_is_a_valid_sdk_state(kind, ent, traits, expected):
    d = device(kind, ent.entity_id, traits=traits)
    state = project_state(d, ent)
    assert state == expected
    validate_device_state(d, state)


def test_sensor_projection_merges_siblings():
    d = device("sensor", "sensor.h:sensor.t", traits=("measure",))
    t = entity("sensor.t", "23.4", device_class="temperature")
    h = entity("sensor.h", "55.6", device_class="humidity")
    assert project_state(d, t, [h]) == {"temp_c": 23.4, "humidity": 56}


def test_service_calls_and_confirmation():
    light = device("light", "light.l", traits=("on_off", "level"))
    on = Command(device_id="d", trait="on_off", command="on")
    call = to_service_call(light, on, {"on": False, "level": 0})
    assert (call.domain, call.service, call.data) == ("light", "turn_on", {"entity_id": "light.l"})
    assert satisfied(on, {"on": False}, {"on": True}) and not satisfied(
        on, {"on": False}, {"on": False}
    )
    step = Command(device_id="d", trait="level", command="step", params={"delta": 30})
    assert to_service_call(light, step, {"on": True, "level": 90}).data["brightness_pct"] == 100
    ac = device(
        "climate", "climate.c", traits=("on_off", "thermostat"), limits=Limits(target_c=(16, 30))
    )
    hotter = Command(device_id="d", trait="thermostat", command="step", params={"delta": 2})
    assert to_service_call(ac, hotter, {"target_c": 29}).data["temperature"] == 30
    mode = Command(device_id="d", trait="thermostat", command="set_mode", params={"mode": "fan"})
    assert to_service_call(ac, mode, {}).data["hvac_mode"] == "fan_only"
    cover = device("cover", "cover.c", traits=("position",))
    assert (
        to_service_call(cover, Command(device_id="d", trait="position", command="open"), {}).service
        == "open_cover"
    )
    assert satisfied(
        Command(device_id="d", trait="position", command="open"), {"position": 0}, {"position": 100}
    )
    media = device("media", "media_player.m", traits=("on_off", "volume"))
    assert (
        to_service_call(
            media, Command(device_id="d", trait="volume", command="set", params={"value": 35}), {}
        ).data["volume_level"]
        == 0.35
    )
    vacuum = device("appliance", "vacuum.v", traits=("operational",))
    assert (
        to_service_call(
            vacuum, Command(device_id="d", trait="operational", command="dock"), {}
        ).service
        == "return_to_base"
    )
