"""The one encoding this Authority must refuse to fingerprint.

`key_id` names the key a voucher is bound to, the key a `device_instance_id` is
derived from, and the key the erase ledger records — each of them compared for
equality against a value some other process computed. So the danger is not a
malformed input that fails loudly. It is an input that produces a *well-formed*
fingerprint for a key nobody meant: nothing fails where the mistake is, and what
surfaces later is a device no Authority has a record of.

`golden/device-instance-derivation.json` named that hazard before this Authority
avoided it. Its `raw-uncompressed-point` case carries the encoding, the reason —
"the natural PSA idiom (psa_export_public_key) and Android's raw EC point both
hand back this encoding" — and, unusually, the wrong answer itself, as
`digest_if_wrongly_hashed`. This Authority used to return exactly that value:
its local decode stripped any scheme prefix and hashed whatever base64url
arrived. `key_id` now goes through eidolon_sdk's `operational_key_id`, which
refuses the encoding instead of fingerprinting it.

The vector supplies both halves, which is the point: the input to refuse, and
the value that must never come back for it.
"""

from __future__ import annotations

import pytest
from golden import golden_vector

from hub.admission.crypto import key_id

VECTOR = "device-instance-derivation.json"


def _raw_uncompressed_point_case() -> dict:
    vector = golden_vector(VECTOR)
    cases = [
        case
        for key, value in vector.items()
        if "refuse" in key.lower() and isinstance(value, list)
        for case in value
        if isinstance(case, dict) and case.get("case") == "raw-uncompressed-point"
    ]
    assert len(cases) == 1, "the vector no longer names the raw-uncompressed-point case"
    return cases[0]


def test_a_raw_uncompressed_point_is_refused_not_fingerprinted() -> None:
    case = _raw_uncompressed_point_case()
    assert case["length_bytes"] == 65
    with pytest.raises(ValueError):
        key_id(case["encoded"])


def test_the_digest_the_vector_publishes_as_wrong_is_never_returned() -> None:
    """The regression the vector was written to make assertable.

    Without this, a future change back to a lax decode would be invisible: it
    would produce a fingerprint of the right shape, for a key no Authority
    holds, and every equality comparison downstream would simply not match.
    """

    case = _raw_uncompressed_point_case()
    forbidden = case["digest_if_wrongly_hashed"].removeprefix("device-instance-")
    try:
        produced = key_id(case["encoded"])
    except ValueError:
        return
    pytest.fail(
        f"key_id fingerprinted a raw uncompressed point as {produced!r}; "
        f"the vector publishes {forbidden!r} as the answer this must never give"
    )


def test_both_contract_spellings_of_one_key_give_one_fingerprint() -> None:
    """The other half of the same rule, and the one that caused an outage.

    Admission records a key as `p256-spki:<base64url>` while a device hands up
    the bare base64url SPKI. They name one key, so they must fingerprint to one
    value — reading only one of them is not a stricter check, it is a wrong one.
    """

    vector = golden_vector(VECTOR)
    spki = vector["spki_der_base64url"]
    assert key_id(spki) == key_id(f"p256-spki:{spki}")
