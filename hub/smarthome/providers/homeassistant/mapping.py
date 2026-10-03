"""Pure functions between Home Assistant entities and the SDK's traits.

No I/O here, so every mapping rule is unit-tested against recorded entity
dictionaries, and the connection code stays small.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from eidolon_sdk.biz.smarthome import (
    ERROR_UNSUPPORTED_COMMAND,
    Command,
    Device,
    DiscoveredDevice,
    Limits,
    SmartHomeError,
    device_traits,
)

# HA hvac modes → SDK thermostat modes. ``off`` is the on_off trait, not a mode.
_HVAC_TO_MODE = {
    "cool": "cool",
    "heat": "heat",
    "auto": "auto",
    "heat_cool": "auto",
    "fan_only": "fan",
    "dry": "dry",
}
_MODE_TO_HVAC = {"cool": "cool", "heat": "heat", "auto": "auto", "fan": "fan_only", "dry": "dry"}
_VACUUM_RUN_STATE = {
    "cleaning": "running",
    "paused": "paused",
    "docked": "docked",
    "returning": "docked",
    "idle": "idle",
    "error": "idle",
}
_UNREACHABLE = {"unavailable", "unknown"}

# Which SDK type an HA domain becomes. Sensors are handled apart (grouped).
_DOMAIN_TYPE = {
    "light": "light",
    "switch": "switch",
    "input_boolean": "switch",
    "climate": "climate",
    "water_heater": "water_heater",
    "cover": "cover",
    "fan": "fan",
    "media_player": "media",
    "vacuum": "appliance",
    "lawn_mower": "appliance",
    "lock": "lock",
    "camera": "camera",
}

# supported_features bits (homeassistant.components.*.const), by domain.
COVER_SET_POSITION = 4
FAN_SET_SPEED = 1
MEDIA_VOLUME_SET = 4


@dataclass(frozen=True, slots=True)
class Entity:
    """What one HA entity looks like to the mapper: its state and registry facts."""

    entity_id: str
    state: str
    attributes: Mapping[str, Any]
    name: str
    area_name: str | None = None
    device_id: str | None = None
    device_class: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.partition(".")[0]


@dataclass(frozen=True, slots=True)
class ServiceCall:
    domain: str
    service: str
    data: dict[str, Any]


# --- discovery ---------------------------------------------------------------


def discover(entities: list[Entity]) -> list[DiscoveredDevice]:
    found: list[DiscoveredDevice] = []
    sensors: dict[str, list[Entity]] = {}
    for entity in entities:
        if entity.domain == "sensor":
            if entity.device_class in ("temperature", "humidity"):
                sensors.setdefault(entity.device_id or entity.entity_id, []).append(entity)
            continue
        kind = _DOMAIN_TYPE.get(entity.domain)
        if kind is None:
            continue
        found.append(
            DiscoveredDevice(
                external_ref=entity.entity_id,
                name=entity.name[:64],
                suggested_type=kind,
                traits=traits_of(entity, kind),
                limits=limits_of(entity, kind),
                area_name=entity.area_name,
                reachable=entity.state not in _UNREACHABLE,
            )
        )
    for group in sensors.values():
        first = group[0]
        found.append(
            DiscoveredDevice(
                external_ref=sensor_ref(group),
                name=(first.name.replace("温度", "").replace("湿度", "").strip() or first.name)[:64]
                if len(group) > 1
                else first.name[:64],
                suggested_type="sensor",
                traits=("measure",),
                area_name=first.area_name,
                reachable=any(e.state not in _UNREACHABLE for e in group),
            )
        )
    return found


def sensor_ref(group: list[Entity]) -> str:
    """One SDK sensor per HA device: the entity ids joined, so it stays an Identifier."""
    return "+".join(sorted(e.entity_id for e in group)).replace("+", ":")


def sensor_entities(ref: str) -> list[str]:
    return ref.split(":")


def traits_of(entity: Entity, kind: str) -> tuple[str, ...]:
    features = int(entity.attributes.get("supported_features") or 0)
    modes = entity.attributes.get("supported_color_modes") or []
    match kind:
        case "light":
            dimmable = "brightness" in entity.attributes or any(m != "onoff" for m in modes)
            return ("on_off", "level") if dimmable else ("on_off",)
        case "cover":
            return ("position",)
        case "fan":
            return ("on_off", "fan_speed") if features & FAN_SET_SPEED else ("on_off",)
        case "media":
            return ("on_off", "volume") if features & MEDIA_VOLUME_SET else ("on_off",)
        case "climate" | "water_heater":
            return ("on_off", "thermostat")
        case "appliance":
            return ("operational",)
        case "lock":
            return ("lock",)
        case "camera" | "switch":
            return ("on_off",)
    return ("on_off",)


def limits_of(entity: Entity, kind: str) -> Limits | None:
    if kind not in ("climate", "water_heater"):
        return None
    attributes = entity.attributes
    low, high = attributes.get("min_temp"), attributes.get("max_temp")
    target = (float(low), float(high)) if low is not None and high is not None else None
    if kind == "climate":
        modes = tuple(
            dict.fromkeys(
                _HVAC_TO_MODE[m] for m in attributes.get("hvac_modes", []) if m in _HVAC_TO_MODE
            )
        )
        return Limits(target_c=target, modes=modes or None)
    return Limits(target_c=target, modes=("heat",))


# --- state --------------------------------------------------------------------


def project_state(device: Device, entity: Entity, extra: list[Entity] = ()) -> dict[str, Any]:
    """The SDK state for ``device`` from the entity (and, for sensors, its siblings)."""
    a = entity.attributes
    traits = set(device_traits(device))
    state: dict[str, Any] = {}
    match device.type:
        case "light":
            state["on"] = entity.state == "on"
            if "level" in traits:
                brightness = a.get("brightness")
                state["level"] = (
                    round(int(brightness) * 100 / 255)
                    if brightness is not None
                    else (100 if state["on"] else 0)
                )
        case "switch" | "camera":
            state["on"] = (
                entity.state == "on"
                if device.type == "switch"
                else entity.state not in _UNREACHABLE | {"off"}
            )
        case "climate":
            state["on"] = entity.state not in _UNREACHABLE | {"off"}
            state["mode"] = _HVAC_TO_MODE.get(entity.state, _first_mode(device))
            state["target_c"] = _num(
                a.get("temperature"), default=a.get("target_temp_high") or a.get("min_temp") or 16
            )
            state["current_c"] = _num_or_none(a.get("current_temperature"))
        case "water_heater":
            state["on"] = entity.state not in _UNREACHABLE | {"off"}
            state["mode"] = "heat"
            state["target_c"] = _num(a.get("temperature"), default=a.get("min_temp") or 35)
            state["current_c"] = _num_or_none(a.get("current_temperature"))
        case "cover":
            position = a.get("current_position")
            if position is None:
                position = 100 if entity.state in ("open", "opening") else 0
            state["position"] = int(position)
        case "fan":
            state["on"] = entity.state == "on"
            if "fan_speed" in traits:
                state["speed"] = int(a.get("percentage") or 0)
        case "media":
            state["on"] = entity.state not in _UNREACHABLE | {"off", "standby"}
            if "volume" in traits:
                state["volume"] = round(float(a.get("volume_level") or 0) * 100)
                state["muted"] = bool(a.get("is_volume_muted") or False)
        case "appliance":
            state["run_state"] = _VACUUM_RUN_STATE.get(entity.state, "idle")
        case "lock":
            state["locked"] = entity.state in ("locked", "locking")
        case "sensor":
            state["temp_c"], state["humidity"] = None, None
            for e in [entity, *extra]:
                value = _num_or_none(e.state)
                if e.device_class == "temperature":
                    state["temp_c"] = value
                elif e.device_class == "humidity":
                    state["humidity"] = None if value is None else int(round(value))
    return state


def _first_mode(device: Device) -> str:
    if device.limits is not None and device.limits.modes:
        return device.limits.modes[0]
    return "cool" if device.type == "climate" else "heat"


# --- commands -----------------------------------------------------------------


def to_service_call(
    device: Device, command: Command, current: Mapping[str, Any], features: int = 0
) -> ServiceCall:
    """The HA service for an SDK command; relative steps are resolved against ``current``.

    ``features`` is the entity's ``supported_features``: the SDK's position trait
    is one unit, but a cover that only opens and closes refuses ``set`` here,
    before Home Assistant is asked.
    """
    domain = device.provider_ref.partition(".")[0] if device.provider_ref else ""
    if (command.trait, command.command) == (
        "position",
        "set",
    ) and not features & COVER_SET_POSITION:
        raise SmartHomeError(ERROR_UNSUPPORTED_COMMAND, "cover has no set_position")
    target = {"entity_id": device.provider_ref}
    p = command.params
    match command.trait, command.command:
        case "on_off", ("on" | "off" | "toggle") as verb:
            return ServiceCall(domain, f"turn_{verb}" if verb != "toggle" else "toggle", target)
        case "level", "set":
            return ServiceCall("light", "turn_on", {**target, "brightness_pct": int(p["value"])})
        case "level", "step":
            level = _clamp(int(current.get("level", 0)) + int(p["delta"]), 0, 100)
            return ServiceCall("light", "turn_on", {**target, "brightness_pct": level})
        case "thermostat", "set_mode":
            return ServiceCall(
                "climate", "set_hvac_mode", {**target, "hvac_mode": _MODE_TO_HVAC[str(p["mode"])]}
            )
        case "thermostat", "set_target":
            return ServiceCall(domain, "set_temperature", {**target, "temperature": p["celsius"]})
        case "thermostat", "step":
            low, high = (
                device.limits.target_c if device.limits and device.limits.target_c else (None, None)
            )
            value = _clamp(float(current.get("target_c", 0)) + float(p["delta"]), low, high)
            return ServiceCall(domain, "set_temperature", {**target, "temperature": value})
        case "fan_speed", "set":
            return ServiceCall("fan", "set_percentage", {**target, "percentage": int(p["value"])})
        case "position", "open":
            return ServiceCall("cover", "open_cover", target)
        case "position", "close":
            return ServiceCall("cover", "close_cover", target)
        case "position", "stop":
            return ServiceCall("cover", "stop_cover", target)
        case "position", "set":
            return ServiceCall(
                "cover", "set_cover_position", {**target, "position": int(p["value"])}
            )
        case "lock", ("lock" | "unlock") as verb:
            return ServiceCall("lock", verb, target)
        case "operational", "start":
            return ServiceCall(domain, "start" if domain == "vacuum" else "start_mowing", target)
        case "operational", "pause":
            return ServiceCall(domain, "pause", target)
        case "operational", "stop":
            return ServiceCall(domain, "stop" if domain == "vacuum" else "dock", target)
        case "operational", "dock":
            return ServiceCall(domain, "return_to_base" if domain == "vacuum" else "dock", target)
        case "volume", "set":
            return ServiceCall(
                "media_player", "volume_set", {**target, "volume_level": int(p["value"]) / 100}
            )
        case "volume", "step":
            volume = _clamp(int(current.get("volume", 0)) + int(p["delta"]), 0, 100)
            return ServiceCall(
                "media_player", "volume_set", {**target, "volume_level": volume / 100}
            )
        case "volume", "mute":
            return ServiceCall(
                "media_player", "volume_mute", {**target, "is_volume_muted": bool(p["muted"])}
            )
    raise SmartHomeError(ERROR_UNSUPPORTED_COMMAND, f"{command.trait}.{command.command}")


def satisfied(command: Command, before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Whether ``after`` shows the command took effect; what the adapter waits for."""
    p = command.params
    match command.trait, command.command:
        case "on_off", "on":
            return after.get("on") is True
        case "on_off", "off":
            return after.get("on") is False
        case "on_off", "toggle":
            return after.get("on") is not None and after.get("on") != before.get("on")
        case "level", "set":
            return (
                after.get("on") is True and abs(int(after.get("level", -1)) - int(p["value"])) <= 1
            )
        case "level", "step":
            return (
                after.get("on") is True
                and after.get("level") != before.get("level")
                or int(p["delta"]) == 0
            )
        case "thermostat", "set_mode":
            return after.get("mode") == p["mode"]
        case "thermostat", "set_target":
            return (
                after.get("target_c") is not None
                and abs(float(after["target_c"]) - float(p["celsius"])) < 0.01
            )
        case "thermostat", "step":
            return after.get("target_c") != before.get("target_c") or float(p["delta"]) == 0
        case "fan_speed", "set":
            return after.get("speed") == int(p["value"])
        case "position", "open":
            return int(after.get("position", -1)) == 100
        case "position", "close":
            return int(after.get("position", -1)) == 0
        case "position", "stop":
            return True
        case "position", "set":
            return int(after.get("position", -1)) == int(p["value"])
        case "lock", "lock":
            return after.get("locked") is True
        case "lock", "unlock":
            return after.get("locked") is False
        case "operational", "start":
            return after.get("run_state") == "running"
        case "operational", "pause":
            return after.get("run_state") == "paused"
        case "operational", "stop":
            return after.get("run_state") in ("idle", "docked")
        case "operational", "dock":
            return after.get("run_state") == "docked"
        case "volume", "set":
            return abs(int(after.get("volume", -1)) - int(p["value"])) <= 1
        case "volume", "step":
            return after.get("volume") != before.get("volume") or int(p["delta"]) == 0
        case "volume", "mute":
            return after.get("muted") == bool(p["muted"])
    return False


def _clamp(value, low, high):
    if low is not None and value < low:
        return low
    if high is not None and value > high:
        return high
    return value


def _num(value, *, default):
    parsed = _num_or_none(value)
    return parsed if parsed is not None else _num_or_none(default) or 0


def _num_or_none(value):
    if value is None or value in _UNREACHABLE:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number
