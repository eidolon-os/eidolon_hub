"""The Authority against the golden bytes of the ClaimGrant AAD.

The AAD is not sent either. It rides on the envelope as JSON, but what the AEAD
tag actually covers is each side's own canonicalisation of it, so the two sides
never compare bytes. A member one side spells differently, orders differently or
serialises differently arrives as a failed tag at the one step of enrolment a
device cannot retry its way out of — indistinguishable from a wrong key, a
corrupted ciphertext, or an attacker.

`eidolon_client_mobile` and `eidolon-client-esp32` are already held to
`golden/claim-grant-aad.json`. This Authority is the third implementation, and
the one that produces the bytes the other two have to reproduce.
"""

from __future__ import annotations

import copy
import hashlib

import pytest
import rfc8785
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from golden import golden_vector

from hub.contracts.bindings.admission import ClaimGrantAAD

VECTOR = "claim-grant-aad.json"


def _canonical(aad: dict) -> bytes:
    """The bytes `seal_claim_grant` authenticates, reached the way it reaches them.

    Through the model, because the Authority seals `aad.model_dump(mode="json")`
    and never the mapping it was handed: a member the model renames, drops or
    re-types would be sealed renamed, dropped or re-typed.
    """

    return rfc8785.dumps(ClaimGrantAAD.model_validate(aad).model_dump(mode="json"))


def test_the_authority_canonicalises_the_aad_to_the_published_bytes() -> None:
    vector = golden_vector(VECTOR)
    canonical = _canonical(vector["aad"])

    assert canonical.decode("utf-8") == vector["canonical_aad_utf8"]
    assert hashlib.sha256(canonical).hexdigest() == vector["canonical_aad_sha256"]


def test_the_published_ciphertext_opens_under_the_authority_s_own_aad_bytes() -> None:
    """The bytes agreeing is the claim; this is that claim as an AEAD tag.

    The vector's ciphertext was produced elsewhere. It opens here only if this
    Authority's canonicalisation is the same string, byte for byte, that the
    firmware and the mobile client compute — which is what a device does when
    it opens a real ClaimGrant.
    """

    vector = golden_vector(VECTOR)
    aead = AESGCM(bytes.fromhex(vector["test_key"]))
    nonce = bytes.fromhex(vector["test_nonce"])
    canonical = _canonical(vector["aad"])

    assert aead.decrypt(nonce, bytes.fromhex(vector["ciphertext"]), canonical) == bytes.fromhex(
        vector["plaintext"]
    )
    assert aead.encrypt(nonce, bytes.fromhex(vector["plaintext"]), canonical).hex() == (
        vector["ciphertext"]
    )


def _other(value):
    """Any value but this one, in whatever shape the member has."""

    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return f"{value}-mutated"
    member = next(iter(value))
    return {**value, member: _other(value[member])}


def test_every_member_of_the_aad_is_authenticated() -> None:
    """A member outside the tag is a member an attacker may choose.

    `hardware_evidence_digest` is the one this matters most for: nothing else
    on the wire binds the sealed Grant to the evidence the Proposal carried, so
    a digest the tag did not cover would be a digest anyone could restate.
    """

    vector = golden_vector(VECTOR)
    members = vector["mutate_each_field_must_fail"]
    assert set(members) == set(vector["aad"]), (
        "the negative matrix and the AAD must name the same members, "
        "or a member is authenticated by nobody's test"
    )

    aead = AESGCM(bytes.fromhex(vector["test_key"]))
    nonce = bytes.fromhex(vector["test_nonce"])
    ciphertext = bytes.fromhex(vector["ciphertext"])
    for member in members:
        mutated = copy.deepcopy(vector["aad"])
        mutated[member] = _other(mutated[member])
        # Canonicalised as JSON rather than through the model: a device rebuilds
        # the AAD from its own state, and `contract` or `profile_id` mutated to
        # anything at all is exactly the state the model would refuse to hold.
        with pytest.raises(InvalidTag):
            aead.decrypt(nonce, ciphertext, rfc8785.dumps(mutated))
