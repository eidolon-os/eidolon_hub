"""Frozen P-256 proof verification and RFC 9180 Base-mode grant sealing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hub.contracts.bindings.admission import (
    ClaimGrantAAD,
    ClaimGrantWireEnvelope,
    operational_key_id,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    raw = value.removeprefix("p256-spki:")
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def load_p256_spki(value: str) -> ec.EllipticCurvePublicKey:
    key = serialization.load_der_public_key(_decode(value))
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("key is not P-256 SPKI")
    return key


def key_id(value: str) -> str:
    """The fingerprint this Authority compares a device's key against.

    eidolon_sdk's, not this module's. The same value names the key a voucher is
    bound to, the key a `device_instance_id` is derived from, and the key the
    erase ledger records — all of them compared for equality against a value
    some other process computed, so a second spelling here is a second identity
    for one key, and the mismatch surfaces as a device nobody has a record of.

    Stricter than the local decode it replaces, deliberately: that one hashed
    whatever base64url arrived, so a raw uncompressed point produced a
    well-formed fingerprint for an identity no Authority holds. This refuses it.
    """

    return operational_key_id(value)


def verify_p256_proof(public_spki: str, document: Any, signature: str) -> bool:
    try:
        raw = _decode(signature)
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        load_p256_spki(public_spki).verify(der, rfc8785.dumps(document), ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature):
        return False


def _extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = b""
    block = b""
    for counter in range(1, math.ceil(length / 32) + 1):
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
    return output[:length]


def _labeled_extract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _extract(salt, b"HPKE-v1" + suite + label + ikm)


def _labeled_expand(suite: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    return _expand(prk, length.to_bytes(2, "big") + b"HPKE-v1" + suite + label + info, length)


def seal_claim_grant(
    public_spki: str,
    grant: dict[str, Any],
    aad: ClaimGrantAAD,
    *,
    recipient_handoff_key_id: str,
) -> ClaimGrantWireEnvelope:
    recipient = load_p256_spki(public_spki)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    enc = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    recipient_point = recipient.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    kem_suite = b"KEM" + (16).to_bytes(2, "big")
    shared = _labeled_expand(
        kem_suite,
        _labeled_extract(kem_suite, b"", b"eae_prk", ephemeral.exchange(ec.ECDH(), recipient)),
        b"shared_secret",
        enc + recipient_point,
        32,
    )
    suite = b"HPKE" + (16).to_bytes(2, "big") + (1).to_bytes(2, "big") + (1).to_bytes(2, "big")
    info = b"eidolon-trust-p256-hpke-v1"
    context = (
        b"\x00"
        + _labeled_extract(suite, b"", b"psk_id_hash", b"")
        + _labeled_extract(suite, b"", b"info_hash", info)
    )
    secret = _labeled_extract(suite, shared, b"secret", b"")
    key = _labeled_expand(suite, secret, b"key", context, 16)
    nonce = _labeled_expand(suite, secret, b"base_nonce", context, 12)
    ciphertext = AESGCM(key).encrypt(
        nonce, rfc8785.dumps(grant), rfc8785.dumps(aad.model_dump(mode="json"))
    )
    return ClaimGrantWireEnvelope(
        recipient_handoff_key_id=recipient_handoff_key_id,
        encapsulated_key=_b64(enc),
        ciphertext=_b64(ciphertext),
        aad=aad,
    )
