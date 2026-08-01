from __future__ import annotations

from datetime import UTC, datetime

from hub.adapters.runtime import SecureIdGenerator, SystemClock


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
