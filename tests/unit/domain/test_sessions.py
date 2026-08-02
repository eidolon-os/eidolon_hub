from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hub.domain.sessions.entities import DeviceSessionLease, DeviceSessionState

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _session(**changes) -> DeviceSessionLease:
    values = {
        "session_id": "session-1",
        "device_id": "device-1",
        "opened_at": NOW,
        "renewed_at": NOW,
        "expires_at": NOW + timedelta(seconds=45),
        "lease_token": "session-token-device-1",
        "identity_fingerprint": "p256:fingerprint",
        "hub_instance_id": "hub-1",
        "fencing_token": 1,
    }
    values.update(changes)
    return DeviceSessionLease(**values)


def test_session_online_state_depends_only_on_lease_and_expiry() -> None:
    session = _session()

    assert session.is_active(NOW)
    assert not session.is_active(NOW + timedelta(seconds=46))
    assert not session.close().is_active(NOW)
    assert session.close().state is DeviceSessionState.CLOSED


def test_session_heartbeat_is_sequenced_and_idempotent() -> None:
    session = _session()
    with pytest.raises(ValueError, match="positive"):
        session.renew(
            now=NOW,
            ttl=timedelta(seconds=45),
            lease_token=session.lease_token,
            sequence=0,
        )
    renewed = session.renew(
        now=NOW + timedelta(seconds=1),
        ttl=timedelta(seconds=45),
        lease_token=session.lease_token,
        sequence=1,
    )

    assert renewed.heartbeat_sequence == 1
    assert renewed.renewed_at == NOW + timedelta(seconds=1)
    assert (
        renewed.renew(
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(seconds=45),
            lease_token=session.lease_token,
            sequence=1,
        )
        is renewed
    )
    with pytest.raises(ValueError, match="stale"):
        renewed.renew(
            now=NOW,
            ttl=timedelta(seconds=45),
            lease_token=session.lease_token,
            sequence=0,
        )


def test_session_rejects_invalid_identity_and_authority_values() -> None:
    session = _session()
    for field_name in (
        "session_id",
        "device_id",
        "lease_token",
        "identity_fingerprint",
        "hub_instance_id",
    ):
        with pytest.raises(ValueError, match=field_name):
            replace(session, **{field_name: ""})
    with pytest.raises(PermissionError, match="mismatch"):
        session.renew(
            now=NOW,
            ttl=timedelta(seconds=45),
            lease_token="wrong-token",
            sequence=1,
        )
    with pytest.raises(ValueError, match="closed"):
        session.close().renew(
            now=NOW,
            ttl=timedelta(seconds=45),
            lease_token=session.lease_token,
            sequence=1,
        )
