"""What this Authority answers, against the vector every Body parses.

The outer object of `configuration:pull` had four readers or writers and no
contract. Its risk is not a parse failure — a missing member fails loudly
enough — it is that `lifecycle_state` and the presence of a channel are two
facts and nothing said so. Approved-with-no-channel is the Authority holding
the Claim while the Channel has not answered; a Body reading it as failure
abandons an enrolment that is fine, and one reading it as active joins a room
that does not exist.

So this holds the producer to the vector's member set for each of the three
states, built through the model the HTTP surface actually returns rather than a
dict assembled here.
"""

from __future__ import annotations

import json
from pathlib import Path

import eidolon_sdk
import pytest

from hub.channel_reconciliation.domain import ChannelBinding
from hub.contracts.bindings.device import DeviceRef, ManifestRef
from hub.device_control.http import DeviceConfigurationResult


def _vector() -> dict:
    path = (
        Path(eidolon_sdk.__file__).resolve().parents[1]
        / "contracts/device_foundation/v1/golden/device-control-configuration-response.json"
    )
    assert path.exists(), f"the canonical response vector is not at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _leaf_paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.extend(_leaf_paths(item, f"{prefix}.{key}" if prefix else str(key)))
        return sorted(found)
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_leaf_paths(item, f"{prefix}[{index}]"))
        return sorted(found)
    return [prefix]


def _built(case: dict, vector: dict) -> dict:
    response = case["response"]
    return DeviceConfigurationResult(
        nonce=vector["request_nonce"],
        device_ref=DeviceRef.model_validate(vector["device_ref"]),
        lifecycle_state=response["lifecycle_state"],
        manifest=(
            ManifestRef.model_validate(response["manifest"])
            if "manifest" in response
            else None
        ),
        channels=tuple(
            # `kinds` is a tuple on this model and validation is strict, so the
            # vector's JSON array has to be converted rather than coerced.
            ChannelBinding.model_validate({**entry, "kinds": tuple(entry["kinds"])})
            for entry in response["channels"]
        ),
    ).model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize("body_state", ["active", "awaiting-channel", "revoked"])
def test_the_authority_answers_the_shape_the_vector_pins(body_state: str) -> None:
    vector = _vector()
    case = next(case for case in vector["cases"] if case["body_state"] == body_state)

    built = _built(case, vector)

    assert _leaf_paths(built) == _leaf_paths(case["response"])
    assert built["lifecycle_state"] == case["response"]["lifecycle_state"]
    assert len(built["channels"]) == len(case["response"]["channels"])


def test_the_three_states_this_authority_can_answer_are_the_three_the_vector_declares() -> None:
    """Not a count for its own sake.

    The vector exists because approved-with-no-channel is a distinct answer.
    A producer that could only ever emit two of these, or a vector that quietly
    lost one, would leave the distinction unchecked with every case above still
    passing.
    """

    vector = _vector()
    assert sorted(case["body_state"] for case in vector["cases"]) == [
        "active",
        "awaiting-channel",
        "revoked",
    ]
    states = {case["response"]["lifecycle_state"] for case in vector["cases"]}
    assert states == {"approved", "revoked"}
    approved_without_channel = [
        case
        for case in vector["cases"]
        if case["response"]["lifecycle_state"] == "approved" and not case["response"]["channels"]
    ]
    assert len(approved_without_channel) == 1
