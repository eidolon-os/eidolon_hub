"""The one permanent hardware identity, derived from verified input only.

`hardware_identity_ref` is the key the Authority keeps `claim_generation`
under, so it outlives every Claim, tombstone and reflash of one board. A
development registry used to let an operator type that value per device, and a
Waveshare ESP32-S3-Touch-AMOLED board was enrolled as
``hardware-box3-1cdbd47aef0c``: a permanent record asserting a board type that
no code verified and that was simply wrong. Board type is a firmware-authored
declaration that belongs in the Manifest, which a device may re-assert; a
hardware identity is an immutable fact and may therefore only be derived.

So the ref is a digest of the hardware lookup id that commissioning actually
verified — the device proved possession of the pre-shared setup secret bound to
exactly that lookup id — and nothing else can be smuggled into its shape.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Domain separation keeps this digest from being confused with the other
#: sha256 identities in Admission (`device-instance-<spki digest>`, key ids),
#: which are computed over rotating operational material and must never be
#: mistaken for a permanent hardware identity.
_DERIVATION_LABEL = b"eidolon-hardware-identity-v1"

_MAXIMUM_LOOKUP_ID_BYTES = 128

#: Full sha256 hex, matching how `device_instance_id` and key ids are shaped in
#: this repository. The shape is what makes a lie unrepresentable: a ref that
#: names a board, a vendor or a room cannot match it.
HARDWARE_IDENTITY_REF_PATTERN = re.compile(r"hardware-[0-9a-f]{64}")


def derive_hardware_identity_ref(device_base_id: str) -> str:
    """Derive the permanent identity of a base identity this Hub issued.

    Case and surrounding whitespace are folded because the input is hex: a
    producer that starts formatting it in upper case must not fork one Body
    into two identity lineages, which would silently restart its Claim
    generations and stop old Claims and tombstones from fencing it. Separators
    are deliberately *not* stripped — collapsing them would let two distinct
    base identities collide into one identity, which is the worse failure.
    """

    canonical = device_base_id.strip().casefold()
    if not canonical or len(canonical.encode()) > _MAXIMUM_LOOKUP_ID_BYTES:
        raise ValueError("device base id is empty or too long to be an identity")
    digest = hashlib.sha256(_DERIVATION_LABEL + b"\0" + canonical.encode()).hexdigest()
    return f"hardware-{digest}"


def require_derived_hardware_identity_ref(hardware_identity_ref: str) -> str:
    """Refuse a ref that was not derived, at the boundary of the durable record.

    Commissioning verifiers are structural: any adapter can be installed, and
    the production board adapter is not written yet. This is the last point
    before a hardware identity becomes permanent history, so an undeliverable
    promise ("this is a box3") is rejected here rather than persisted forever.
    """

    if HARDWARE_IDENTITY_REF_PATTERN.fullmatch(hardware_identity_ref) is None:
        raise ValueError("hardware identity ref was not derived from verified input")
    return hardware_identity_ref


@dataclass(frozen=True, slots=True)
class VerifiedBaseIdentity:
    """The only identity fact a commissioning adapter may assert.

    An adapter returns the base identity it verified — one this Owner Domain
    issued and bound to the key the device just proved it holds — and never the
    identity ref itself, so no adapter can choose the shape or the content of
    something that outlives every Claim. A value the *device* proposed can
    never arrive here: the device has no say in what it is called.
    """

    device_base_id: str

    def hardware_identity_ref(self) -> str:
        return derive_hardware_identity_ref(self.device_base_id)
