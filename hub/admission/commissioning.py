"""Commissioning proofs: what gives a Body standing to ask for admission.

A device carries no factory identity material at all. Its base identity is
minted by the Hub during a Controller-witnessed commissioning, delivered over
that session, and bound one-to-one to the operational key the device generated
for itself. Two consequences shape this module:

* one firmware image is valid for every unit of a model, because there is
  nothing per-device in it to differ; and
* the Hub keeps no per-device secret file, because the signature on the voucher
  is what proves this Owner Domain issued the identity.

The predecessor mechanism pre-shared a per-device setup secret between a
firmware image and a root-owned registry file on the Host. Two ledgers for one
fact drifted the way two ledgers do: a registry entry once welded an
unverifiable board type into a permanent identity, and a rollback across that
file's format left the Hub restarting 110 times. Neither failure has anywhere
to live now.

Everything here is pure verification. One-shot consumption of a voucher, the
base-identity binding and the state of a Claim are durable facts, so they belong
to the Authority that owns the transaction, not to a credential parser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

import rfc8785

from hub.admission.crypto import key_id, verify_p256_proof
from hub.admission.hardware_identity import VerifiedBaseIdentity

# Everything a voucher's signed bytes consist of is eidolon_sdk's, not this
# module's: the derivation, the claim set, the header and the accepted
# provenance values. Admin assembles and signs those bytes and this Hub takes
# them apart, without either side ever comparing an intermediate value — so a
# second spelling of any of the four would surface only as a device refused at
# the first commissioning it cannot retry past, with the refusal naming a
# signature or nothing at all. `derive_voucher_signing_key`,
# `commissioning_voucher_claims` and `sign_commissioning_voucher` are
# re-exported rather than merely imported: this Hub's composition and the tests
# that sign for it reach them through this module.
from hub.contracts.bindings.admission import (  # noqa: F401
    COMMISSIONING_VOUCHER_CLAIM_NAMES as _VOUCHER_CLAIMS,
)
from hub.contracts.bindings.admission import (  # noqa: F401
    COMMISSIONING_VOUCHER_HEADER as _VOUCHER_HEADER,
)
from hub.contracts.bindings.admission import (  # noqa: F401
    COMMISSIONING_VOUCHER_PROVENANCE as _PROVENANCE,
)
from hub.contracts.bindings.admission import (  # noqa: F401
    COMMISSIONING_VOUCHER_PURPOSE as VOUCHER_PURPOSE,
)
from hub.contracts.bindings.admission import (  # noqa: F401
    commissioning_voucher_claims,
    derive_voucher_signing_key,
    sign_commissioning_voucher,
)

VOUCHER_SCHEME = "hub-issued-commissioning-voucher-v1"
ENROLLED_BASE_KEY_SCHEME = "enrolled-base-key-v1"
BASE_IDENTITY_EVIDENCE_SCHEME = "hub-issued-base-p256"
ENROLLED_BASE_KEY_CONTRACT = "eidolon.device-foundation.enrolled-base-key-v1"

_EVIDENCE_FIELDS = frozenset(
    {
        "device_base_id",
        "device_instance_id",
        "operational_public_key",
        "profile_id",
    }
)
_PROFILE_ID = "eidolon-trust-p256-hpke-v1"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class VerifiedCommissioning:
    """What a proof established, and what the Authority still has to enforce.

    `jti` is present exactly when a voucher was presented, because that is the
    value the durable one-shot ledger consumes. A verifier that consumed it here
    would be deciding a transaction outcome from inside a signature check.
    """

    identity: VerifiedBaseIdentity
    scheme: str
    jti: str | None = None
    expires_at: int | None = None


class CommissioningProofVerifier(Protocol):
    def verify(
        self,
        *,
        device_instance_id: str,
        owner_domain_id: str,
        nonce: str,
        proof: str,
        hardware_identity_evidence: dict,
        operational_public_key: str,
        now_unix: int,
    ) -> VerifiedCommissioning | None: ...


class IssuedBaseIdentityVerifier:
    """Verify possession of the key an issued base identity is bound to.

    Both schemes prove the same thing about the device — it holds that key — and
    differ in what gives it standing: a voucher this Owner Domain signed during
    a witnessed commissioning, or a binding this Owner Domain already recorded.
    """

    def __init__(self, voucher_signing_key: bytes) -> None:
        self._voucher_signing_key = voucher_signing_key

    def verify(
        self,
        *,
        device_instance_id: str,
        owner_domain_id: str,
        nonce: str,
        proof: str,
        hardware_identity_evidence: dict,
        operational_public_key: str,
        now_unix: int,
    ) -> VerifiedCommissioning | None:
        device_base_id = self._verified_evidence(
            hardware_identity_evidence,
            device_instance_id=device_instance_id,
            operational_public_key=operational_public_key,
        )
        if device_base_id is None:
            return None
        return self._verified_standing(
            device_base_id=device_base_id,
            device_instance_id=device_instance_id,
            owner_domain_id=owner_domain_id,
            nonce=nonce,
            proof=proof,
            operational_public_key=operational_public_key,
            now_unix=now_unix,
        )

    def _verified_evidence(
        self,
        evidence: dict,
        *,
        device_instance_id: str,
        operational_public_key: str,
    ) -> str | None:
        if evidence.get("scheme") != BASE_IDENTITY_EVIDENCE_SCHEME:
            return None
        try:
            document_raw, signature = str(evidence["evidence"]).rsplit(".", 1)
            document = json.loads(document_raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or set(document) != _EVIDENCE_FIELDS:
            return None
        # The canonical form is re-derived rather than trusted: a device that
        # signs one byte sequence and sends another would otherwise be verified
        # against the sequence it chose to show.
        if document_raw != rfc8785.dumps(document).decode():
            return None
        if (
            document["device_instance_id"] != device_instance_id
            or document["operational_public_key"] != operational_public_key
            or document["profile_id"] != _PROFILE_ID
            or not verify_p256_proof(operational_public_key, document, signature)
        ):
            return None
        return str(document["device_base_id"])

    def _verified_standing(
        self,
        *,
        device_base_id: str,
        device_instance_id: str,
        owner_domain_id: str,
        nonce: str,
        proof: str,
        operational_public_key: str,
        now_unix: int,
    ) -> VerifiedCommissioning | None:
        voucher = self._verified_voucher(
            proof,
            device_base_id=device_base_id,
            owner_domain_id=owner_domain_id,
            nonce=nonce,
            operational_public_key=operational_public_key,
            now_unix=now_unix,
        )
        if voucher is not None:
            return voucher
        return self._verified_continuation(
            proof,
            device_base_id=device_base_id,
            device_instance_id=device_instance_id,
            owner_domain_id=owner_domain_id,
            nonce=nonce,
            operational_public_key=operational_public_key,
        )

    def _verified_voucher(
        self,
        proof: str,
        *,
        device_base_id: str,
        owner_domain_id: str,
        nonce: str,
        operational_public_key: str,
        now_unix: int,
    ) -> VerifiedCommissioning | None:
        parts = proof.split(".")
        if len(parts) != 3:
            return None
        signing_input = f"{parts[0]}.{parts[1]}"
        try:
            expected = hmac.new(
                self._voucher_signing_key, signing_input.encode(), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected, _b64url_decode(parts[2])):
                return None
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if header != _VOUCHER_HEADER:
            return None
        if not isinstance(claims, dict) or set(claims) != _VOUCHER_CLAIMS:
            return None
        if (
            claims["purpose"] != VOUCHER_PURPOSE
            or claims["owner_domain_id"] != owner_domain_id
            or claims["device_base_id"] != device_base_id
            or claims["base_identity_provenance"] not in _PROVENANCE
            or claims["jti"] != nonce
        ):
            return None
        # The binding to one operational key is the whole of a voucher's value:
        # without it, anything that reads the token once could exchange it for
        # standing under a key of its own.
        if claims["operational_spki_sha256"] != key_id(operational_public_key):
            return None
        try:
            expires_at = int(claims["exp"])
        except (TypeError, ValueError):
            return None
        if expires_at <= now_unix:
            return None
        return VerifiedCommissioning(
            identity=VerifiedBaseIdentity(device_base_id),
            scheme=VOUCHER_SCHEME,
            jti=str(claims["jti"]),
            expires_at=expires_at,
        )

    def _verified_continuation(
        self,
        proof: str,
        *,
        device_base_id: str,
        device_instance_id: str,
        owner_domain_id: str,
        nonce: str,
        operational_public_key: str,
    ) -> VerifiedCommissioning | None:
        document = {
            "contract": ENROLLED_BASE_KEY_CONTRACT,
            "device_base_id": device_base_id,
            "device_instance_id": device_instance_id,
            "nonce": nonce,
            "owner_domain_id": owner_domain_id,
        }
        if not verify_p256_proof(operational_public_key, document, proof):
            return None
        return VerifiedCommissioning(
            identity=VerifiedBaseIdentity(device_base_id),
            scheme=ENROLLED_BASE_KEY_SCHEME,
        )


class RejectingCommissioningProofVerifier:
    """Fail closed until the deployment supplies the voucher signing key.

    The canonical Admission route may be the production route before a Host has
    been configured to sign vouchers; an unconfigured Hub must never turn an
    opaque string into a trusted Proposal.
    """

    def verify(
        self,
        *,
        device_instance_id: str,
        owner_domain_id: str,
        nonce: str,
        proof: str,
        hardware_identity_evidence: dict,
        operational_public_key: str,
        now_unix: int,
    ) -> VerifiedCommissioning | None:
        del (
            device_instance_id,
            owner_domain_id,
            nonce,
            proof,
            hardware_identity_evidence,
            operational_public_key,
            now_unix,
        )
        return None
