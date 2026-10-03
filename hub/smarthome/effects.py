"""What a command is expected to do to a device's state, in SDK vocabulary only.

Two users: an event-confirming Provider waits until the ecosystem's state shows
the effect; the runtime reconciles an ``unknown`` receipt when a later
observation shows it. Both answer the same question, so it is written once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eidolon_sdk.biz.smarthome import Command


def satisfied(command: Command, before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
    """Whether ``after`` shows ``command`` took effect; ``before`` disambiguates toggles and steps."""
    p = command.params
    before = before or {}
    match command.trait, command.command:
        case "on_off", "on":
            return after.get("on") is True
        case "on_off", "off":
            return after.get("on") is False
        case "on_off", "toggle":
            return after.get("on") is not None and after.get("on") != before.get("on")
        case "level", "set":
            return after.get("on") is True and _near(after.get("level"), p["value"])
        case "level", "step":
            return int(p["delta"]) == 0 or (
                after.get("on") is True and after.get("level") != before.get("level")
            )
        case "thermostat", "set_mode":
            return after.get("mode") == p["mode"]
        case "thermostat", "set_target":
            return (
                after.get("target_c") is not None
                and abs(float(after["target_c"]) - float(p["celsius"])) < 0.01
            )
        case "thermostat", "step":
            return float(p["delta"]) == 0 or after.get("target_c") != before.get("target_c")
        case "fan_speed", "set":
            return after.get("speed") == int(p["value"])
        case "position", "open":
            return after.get("position") == 100
        case "position", "close":
            return after.get("position") == 0
        case "position", "stop":
            return "position" in after
        case "position", "set":
            return after.get("position") == int(p["value"])
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
            return _near(after.get("volume"), p["value"])
        case "volume", "step":
            return int(p["delta"]) == 0 or after.get("volume") != before.get("volume")
        case "volume", "mute":
            return after.get("muted") == bool(p["muted"])
    return False


def _near(actual: Any, expected: Any, tolerance: int = 1) -> bool:
    try:
        return abs(int(actual) - int(expected)) <= tolerance
    except (TypeError, ValueError):
        return False
