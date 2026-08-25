"""The shape of a hardware identity must not be able to assert a lie."""

from __future__ import annotations

import hashlib

import pytest

from hub.admission.hardware_identity import (
    HARDWARE_IDENTITY_REF_PATTERN,
    derive_hardware_identity_ref,
)
from hub.admission.persistence import SqlAdmissionStore


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def test_ref_is_derived_from_the_verified_lookup_and_carries_nothing_else() -> None:
    lookup_id = "1c:db:d4:7a:ef:0c"
    ref = derive_hardware_identity_ref(lookup_id)

    assert HARDWARE_IDENTITY_REF_PATTERN.fullmatch(ref)
    assert ref == "hardware-" + hashlib.sha256(
        b"eidolon-hardware-identity-v1\x001c:db:d4:7a:ef:0c"
    ).hexdigest()


def test_ref_cannot_smuggle_a_board_type_out_of_the_lookup_id() -> None:
    # The real defect: an operator typed "hardware-box3-1cdbd47aef0c" for a
    # Waveshare AMOLED board, and the Claim then asserted "box3" forever with
    # nothing able to check it. A derived ref cannot carry that claim even when
    # the input string tries to.
    ref = derive_hardware_identity_ref("box3-1c:db:d4:7a:ef:0c")

    assert "box3" not in ref
    assert HARDWARE_IDENTITY_REF_PATTERN.fullmatch(ref)


def test_hand_written_refs_are_not_of_the_derived_shape() -> None:
    for lie in (
        "hardware-box3-1cdbd47aef0c",
        "hardware-box-3-hil",
        "hardware-1cdbd47aef0c",
        "hardware-" + "F" * 64,
        "hardware-" + "a" * 63,
    ):
        assert HARDWARE_IDENTITY_REF_PATTERN.fullmatch(lie) is None


def test_ref_is_stable_for_one_board_and_distinct_between_boards() -> None:
    # Stability is the whole point: claim_generation is kept per
    # (owner_domain_id, hardware_identity_ref), so a ref that moves after a
    # reflash would let an old Claim and its tombstone stop fencing the board.
    assert derive_hardware_identity_ref("24:ec:4a:52:f3:54") == derive_hardware_identity_ref(
        "24:EC:4A:52:F3:54"
    )
    assert derive_hardware_identity_ref("24:ec:4a:52:f3:54") == derive_hardware_identity_ref(
        " 24:ec:4a:52:f3:54\n"
    )
    assert derive_hardware_identity_ref("24:ec:4a:52:f3:54") != derive_hardware_identity_ref(
        "1c:db:d4:7a:ef:0c"
    )


def test_an_unusable_lookup_id_is_refused_instead_of_hashed() -> None:
    for unusable in ("", "   ", "a" * 129):
        with pytest.raises(ValueError):
            derive_hardware_identity_ref(unusable)


def test_a_proposal_cannot_persist_a_ref_that_was_not_derived() -> None:
    # The Proposal is where a hardware identity first becomes a durable,
    # cross-generation fact, so it is the boundary that has to refuse a
    # hand-written ref no matter which adapter produced it.
    with pytest.raises(ValueError):
        SqlAdmissionStore.add_proposal(
            _RecordingSession(),
            enrollment_id="enrollment_01",
            device_instance_id="device-instance-01",
            hardware_identity_ref="hardware-box3-1cdbd47aef0c",
        )

    session = _RecordingSession()
    SqlAdmissionStore.add_proposal(
        session,
        enrollment_id="enrollment_01",
        device_instance_id="device-instance-01",
        hardware_identity_ref=derive_hardware_identity_ref("1c:db:d4:7a:ef:0c"),
    )
    assert len(session.added) == 1
