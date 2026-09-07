"""The Authority against the golden bytes of the two Device Control proofs.

Same shape as the ClaimGrant proofs and one step later on the chain, with a
worse symptom. These are the requests behind `configuration:pull` and
`manifest:assert` — the first is how a Body is handed its channel. A device
that spells the document differently is refused for a signature that did not
verify, holds a Claim this Authority reports as active, and never gets a room;
"active with no channel" is a state the product cannot currently tell apart
from waiting.

Read from the installed SDK rather than restated here, for the reason the
vectors exist at all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import eidolon_sdk
import pytest
import rfc8785
from eidolon_sdk.device_foundation.v1 import (
    DeviceRef,
    ManifestDocument,
    device_control_configuration_proof_document,
)

from hub.contracts.bindings.device import AssertDeviceManifest, verify_p256_signature


def _vector(name: str) -> dict:
    path = (
        Path(eidolon_sdk.__file__).resolve().parents[1]
        / "contracts/device_foundation/v1/golden"
        / name
    )
    assert path.exists(), f"the canonical proof vector is not at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _configuration_document(vector: dict) -> dict:
    return device_control_configuration_proof_document(
        device_ref=DeviceRef.model_validate(vector["document"]["device_ref"]),
        nonce=vector["document"]["nonce"],
    )


def _assertion_document(vector: dict) -> dict:
    """Through the binding this Authority actually verifies against.

    `AcceptDeviceManifest` calls `assertion.signing_document()` on the parsed
    wire object, so that is what has to reproduce the vector — not a dict
    assembled here, which would agree with the vector and prove nothing.
    """

    asserted = vector["document"]
    manifest_cases = json.loads(
        (
            Path(eidolon_sdk.__file__).resolve().parents[1]
            / "contracts/device_foundation/v1/golden/device-manifest.json"
        ).read_text(encoding="utf-8")
    )["cases"]
    case = next(
        case for case in manifest_cases if case["digest"] == asserted["manifest_digest"]
    )
    return AssertDeviceManifest(
        device_ref=DeviceRef.model_validate(asserted["device_ref"]),
        manifest=ManifestDocument(
            manifest_id="manifest_01",
            revision=1,
            digest=asserted["manifest_digest"],
            document=json.loads(case["canonical_utf8"]),
        ),
        nonce=asserted["nonce"],
        public_key_spki=vector["public_key_spki"],
        device_signature=vector["signature"],
    ).signing_document()


VECTORS = (
    ("device-control-configuration-proof.json", _configuration_document),
    ("device-control-manifest-assertion-proof.json", _assertion_document),
)


@pytest.mark.parametrize(("name", "build"), VECTORS)
def test_the_authority_verifies_the_proof_the_vector_publishes(name: str, build) -> None:
    vector = _vector(name)
    rebuilt = build(vector)

    assert rfc8785.dumps(rebuilt).decode("utf-8") == vector["canonical_utf8"]
    # This Authority's own verifier, over a document this Authority built,
    # accepting a signature produced somewhere else entirely.
    verify_p256_signature(
        public_key_spki=vector["public_key_spki"],
        signing_document=rebuilt,
        signature=vector["signature"],
    )


@pytest.mark.parametrize(("name", "build"), VECTORS)
def test_a_proof_over_any_other_document_is_refused(name: str, build) -> None:
    vector = _vector(name)
    members = vector["mutate_each_field_must_fail"]
    assert members, "a vector with no negative matrix asserts nothing about what is refused"

    for member in members:
        mutated = copy.deepcopy(vector["document"])
        target = mutated
        *parents, last = member.split(".")
        for step in parents:
            target = target[step]
        value = target[last]
        target[last] = value + 1 if isinstance(value, int) else f"{value}-mutated"
        with pytest.raises(Exception, match="signature verification failed"):
            verify_p256_signature(
                public_key_spki=vector["public_key_spki"],
                signing_document=mutated,
                signature=vector["signature"],
            )
