"""The Authority against the golden bytes of the two proofs it verifies.

Neither proof document is ever sent. The device builds it, signs it, and sends
only the signature; the Authority rebuilds it from the Proposal it holds and
verifies. So the two implementations never compare documents — a disagreement
surfaces as an unverifiable proof at the one step of enrolment a device cannot
retry its way out of, with nothing anywhere naming the field they spelled
differently. The entry contract typed both proofs as
`{"type": "string", "minLength": 16}`, which is true of any sixteen bytes.

These vectors are what the firmware and this Authority are now both held to.
Read from the installed SDK rather than restated here, for the reason the
vectors exist at all.
"""

from __future__ import annotations

import copy

import pytest
import rfc8785
from eidolon_sdk.device_foundation.v1 import (
    DeviceRef,
    claim_grant_ack_proof_document,
    claim_grant_collection_proof_document,
)
from golden import golden_vector

from hub.admission.crypto import verify_p256_proof


def _rebuilt(vector: dict) -> dict:
    """The document this Authority builds, from the vector's own inputs.

    Built through the canonical helpers because that is what `collect_claim_grant`
    and `ack_claim_grant` call: a copy assembled here would agree with the vector
    and prove nothing about what the Authority verifies against.
    """

    document = vector["document"]
    if document["contract"].endswith("claim-grant-collection"):
        return claim_grant_collection_proof_document(
            enrollment_id=document["enrollment_id"],
            proposal_revision=document["proposal_revision"],
            collection_challenge=document["collection_challenge"],
        )
    return claim_grant_ack_proof_document(
        enrollment_id=document["enrollment_id"],
        grant_id=document["grant_id"],
        device_ref=DeviceRef.model_validate(document["device_ref"]),
    )


VECTORS = ("claim-grant-collection-proof.json", "claim-grant-ack-proof.json")


@pytest.mark.parametrize("name", VECTORS)
def test_the_authority_verifies_the_proof_the_vector_publishes(name: str) -> None:
    vector = golden_vector(name)
    rebuilt = _rebuilt(vector)

    assert rfc8785.dumps(rebuilt).decode("utf-8") == vector["canonical_utf8"]
    # And the whole point: this Authority's own verifier, over a document this
    # Authority built, accepting a signature made somewhere else entirely.
    assert verify_p256_proof(vector["public_key_spki"], rebuilt, vector["signature"])
    assert verify_p256_proof(
        "p256-spki:" + vector["public_key_spki"], rebuilt, vector["signature"]
    ), "the Authority stores this key with its scheme prefix and must verify it either way"


@pytest.mark.parametrize("name", VECTORS)
def test_a_proof_over_any_other_document_is_refused(name: str) -> None:
    vector = golden_vector(name)
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
        assert not verify_p256_proof(
            vector["public_key_spki"], mutated, vector["signature"]
        ), f"{member} can be changed without invalidating the proof"


def test_the_acknowledgement_is_signed_by_the_device_it_names() -> None:
    """The operational key and `device_instance_id` are one statement, not two.

    A proof by any other key would be some device making another device's Claim
    active. The Authority verifies against the Proposal's stored operational
    key, so this is the property the stored key has to have.
    """

    from eidolon_sdk.device_foundation.v1 import derive_device_instance_id

    vector = golden_vector("claim-grant-ack-proof.json")
    assert vector["document"]["device_ref"]["device_instance_id"] == derive_device_instance_id(
        vector["public_key_spki"]
    )
