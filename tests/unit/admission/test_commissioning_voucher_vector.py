"""This Hub against a commissioning voucher minted outside this repository.

The voucher signing key is never sent. Admin derives it from the Owner Domain's
management secret to sign, and this Hub derives it again to verify, so the two
never compare intermediate values. A disagreement about that derivation is
therefore not reported as a disagreement: it arrives as a device refused at
first commissioning, holding a voucher with a perfectly valid signature by a
key this Hub did not compute, and no field anywhere naming what differs.

Both sides now call `derive_voucher_signing_key` from eidolon_sdk, so there is
one definition rather than two that agree. This test closes the loop from the
verifying end: the token below was minted by the SDK's own generator, from the
management secret the vector states, and is verified here by the production
`IssuedBaseIdentityVerifier`. Admin's suite asserts its issuer reproduces these
same bytes, so the two halves are held to one artifact neither of them wrote.
"""

from __future__ import annotations

from golden import golden_vector

from hub.admission.commissioning import (
    IssuedBaseIdentityVerifier,
    derive_voucher_signing_key,
)

VECTOR = "commissioning-voucher.json"


def _evidence(vector: dict) -> dict:
    return {
        "scheme": vector["evidence_scheme"],
        "evidence": vector["wire_evidence"],
        "evidence_digest": vector["evidence_digest"],
    }


def _verify(vector: dict, signing_key: bytes, *, key: str = "voucher"):
    voucher = vector[key]
    return IssuedBaseIdentityVerifier(signing_key).verify(
        device_instance_id=vector["device_instance_id"],
        owner_domain_id=vector["owner_domain_id"],
        nonce=voucher["jti"],
        proof=voucher["compact"],
        hardware_identity_evidence=_evidence(vector),
        operational_public_key=vector["operational_public_key"],
        now_unix=vector["voucher"]["expires_at_unix"] - 1,
    )


def test_derivation_matches_the_published_signing_key() -> None:
    """The bytes, not the description of them.

    A change to the `info` string, the length or the hash shows up here as a
    vector mismatch naming the derivation, which is the whole point: the
    alternative symptom is a 401 on every first commissioning in the fleet.
    """

    voucher = golden_vector(VECTOR)["voucher"]
    derived = derive_voucher_signing_key(
        bytes.fromhex(voucher["host_management_secret_hex"])
    )
    assert derived.hex() == voucher["signing_key_hex"]


def test_verifies_a_voucher_this_repository_did_not_mint() -> None:
    """Standing established from bytes signed outside this suite.

    Hub's own tests mint vouchers with Hub's own helper, which proves the
    verifier agrees with itself. This one is signed by the SDK generator, so it
    fails if Hub's derivation, claim set, canonicalisation or compact framing
    moves away from the published contract.
    """

    vector = golden_vector(VECTOR)
    signing_key = derive_voucher_signing_key(
        bytes.fromhex(vector["voucher"]["host_management_secret_hex"])
    )
    verified = _verify(vector, signing_key)
    assert verified is not None
    assert verified.scheme == "hub-issued-commissioning-voucher-v1"
    assert verified.jti == vector["voucher"]["jti"]
    assert verified.identity.device_base_id == vector["device_base_id"]
    # The ref that outlives every Claim, derived by this Hub and pinned by the
    # vector — the device has no say in it, so the two must agree exactly.
    assert verified.identity.hardware_identity_ref() == vector["hardware_identity_ref"]


def test_a_key_from_another_secret_refuses_the_same_voucher() -> None:
    """The derived key is load-bearing, not incidental.

    Without this, the positive above would still pass if the verifier had
    stopped checking the signature at all.
    """

    vector = golden_vector(VECTOR)
    other = derive_voucher_signing_key(b"a-different-owner-domain-management-secret")
    assert _verify(vector, other) is None


def test_accepts_the_other_provenance_the_contract_publishes() -> None:
    """`derived-from-controller`, which nothing else in this suite exercises.

    This is the standing a Body returns with: the Hub re-signs a base identity
    it already issued to this very key, which is what lets a removed Body come
    back as itself rather than as a stranger. Every other voucher in these
    tests is `minted`, so without this the accepted set could quietly shrink to
    one value and every suite would stay green while no return was possible.
    """

    vector = golden_vector(VECTOR)
    signing_key = derive_voucher_signing_key(
        bytes.fromhex(vector["voucher"]["host_management_secret_hex"])
    )
    continuation = vector["voucher_derived_from_controller"]
    assert continuation["claims"]["base_identity_provenance"] == "derived-from-controller"

    verified = _verify(vector, signing_key, key="voucher_derived_from_controller")
    assert verified is not None
    assert verified.jti == continuation["jti"]
    assert verified.identity.device_base_id == vector["device_base_id"]
    # A one-shot token: the two vouchers must not be interchangeable.
    assert continuation["jti"] != vector["voucher"]["jti"]
