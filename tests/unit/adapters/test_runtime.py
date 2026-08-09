from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from hub.adapters.runtime import LocalProcessLock, SecureIdGenerator, SystemClock


def test_system_clock_returns_current_utc_time() -> None:
    before = datetime.now(UTC)
    actual = SystemClock().now()
    after = datetime.now(UTC)

    assert actual.tzinfo is UTC
    assert before <= actual <= after


def test_secure_id_generator_returns_unique_prefixed_identifiers() -> None:
    ids = SecureIdGenerator()

    first = ids.new("connection")
    second = ids.new("connection")

    assert first.startswith("connection_")
    assert second.startswith("connection_")
    assert first != second


def test_enrollment_identifier_fits_physical_pairing_qr_profile() -> None:
    enrollment_id = SecureIdGenerator().new("enrollment")

    assert re.fullmatch(r"[A-Za-z0-9_-]+", enrollment_id)
    assert len(enrollment_id) == len("enrollment_") + 24
    # EIDOLON:PAIR:1:<enrollment_id>:<43-char ESP base64url secret>
    assert 15 + len(enrollment_id) + 1 + 43 <= 106


def test_local_process_lock_rejects_a_second_hub_and_is_recoverable(tmp_path) -> None:
    first = LocalProcessLock(tmp_path / "hub.sqlite3.lock")
    second = LocalProcessLock(tmp_path / "hub.sqlite3.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another Eidolon Hub process"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
