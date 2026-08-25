from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from hub.adapters.persistence.channel_reconciliation import SqlChannelRevocationStore
from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import (
    AdmissionClaimEventStreamRow,
    ChannelRevocationDeliveryRow,
)
from hub.channel_reconciliation.application import (
    ReconcileChannelBinding,
    ReconcileChannelRevocations,
)
from hub.channel_reconciliation.domain import (
    ChannelBinding,
    ChannelProviderError,
    ChannelProviderUnavailable,
    CurrentChannelBinding,
)
from hub.channel_reconciliation.provider_client import ChannelProviderHttpClient
from hub.contracts.bindings.device import DeviceRef
from hub.domain.devices.entities import DeviceLifecycleState, ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
REF = DeviceRef(
    device_instance_id="device_channel_01",
    owner_domain_id="owner-domain_01",
    owner_domain_generation=2,
    claim_generation=7,
    trust_epoch=4,
)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _manifest() -> DeviceManifestDocument:
    return DeviceManifestDocument.from_declaration(
        document={
            "schema_version": 1,
            "title": "BOX-3",
            "properties": [],
            "actions": [],
            "events": [],
            "media": [
                {
                    "kind": "audio",
                    "direction": "bidirectional",
                    "codecs": ["audio/opus"],
                }
            ],
        },
        declared_revision=1,
    )


def _canonical_device_manifest() -> DeviceManifestDocument:
    """What a real board sends: its own vocabulary, not this Authority's."""

    return DeviceManifestDocument.from_declaration(document={"endpoints": []}, declared_revision=1)


def _device() -> ManagedDevice:
    return ManagedDevice(
        identity=DeviceIdentity(REF.device_instance_id),
        display_name="BOX-3",
        device_kind="esp-box-3",
        manifest=_manifest(),
        enrolled_at=NOW,
        updated_at=NOW,
        owner_domain_id=str(REF.owner_domain_id),
        owner_domain_generation=REF.owner_domain_generation,
        claim_generation=REF.claim_generation,
        trust_epoch=REF.trust_epoch,
        owner_id="owner_01",
        lifecycle_state=DeviceLifecycleState.APPROVED,
    )


class DeviceReader:
    async def get(self, device_id: str):
        return _device() if device_id == REF.device_instance_id else None


def _channel(expires_at_ms: int) -> tuple[ChannelBinding, ...]:
    return (
        ChannelBinding(
            channel_id="channel_01",
            purpose="device-session",
            kinds=("audio",),
            binding_format="application/vnd.eidolon.livekit-session+json;v=2",
            issued_at_ms=int(NOW.timestamp() * 1000),
            expires_at_ms=expires_at_ms,
            opaque_binding="e30=",
        ),
    )


class RecordingProvider:
    """A fake that keeps the real Provider ledger's rules, including fencing.

    A fake that answered every ``provision`` hid the defect completely: the
    real ledger fences the provision row when a refresh lands, so replaying
    that operation id is refused forever. Without that rule here, the sequence
    that stranded every device two hours after enrolment passed in tests.
    """

    def __init__(self) -> None:
        self.provisions: list[dict] = []
        self.refreshes: list[dict] = []
        self.revocations: list[dict] = []
        self.reads = 0
        self.offline = False
        self.stale_revoke = False
        self._binding: CurrentChannelBinding | None = None
        self._fenced: set[str] = set()

    def seed_expired_binding(self, operation_id: str = "channel-provision-seeded") -> None:
        self._binding = CurrentChannelBinding(
            operation_id=operation_id,
            manifest_revision=_device().manifest.digest,
            channels=_channel(int(NOW.timestamp() * 1000)),
        )

    async def current(self, *, device_ref):
        self.reads += 1
        if self.offline:
            raise ChannelProviderUnavailable()
        return self._binding

    async def provision(self, **values):
        self.provisions.append(values)
        if self.offline:
            raise ChannelProviderUnavailable()
        if values["operation_id"] in self._fenced:
            raise ChannelProviderError(
                "INVALID_TRANSITION",
                retryable=False,
                detail="the operation belongs to a terminal fenced lifecycle",
            )
        return self._establish(values, NOW + timedelta(minutes=30))

    async def refresh(self, **values):
        self.refreshes.append(values)
        if self.offline:
            raise ChannelProviderUnavailable()
        # Landing a refresh ends the operation it advances past.
        if self._binding is not None:
            self._fenced.add(self._binding.operation_id)
        return self._establish(values, NOW + timedelta(minutes=30))

    def _establish(self, values: dict, expiry) -> tuple[ChannelBinding, ...]:
        channels = _channel(int(expiry.timestamp() * 1000))
        self._binding = CurrentChannelBinding(
            operation_id=values["operation_id"],
            manifest_revision=values["manifest_revision"],
            channels=channels,
        )
        return channels

    async def revoke(self, **values):
        self.revocations.append(values)
        if self.offline:
            raise ChannelProviderUnavailable()
        if self.stale_revoke:
            raise ChannelProviderError("STALE_GENERATION", retryable=False)


@pytest.mark.asyncio
async def test_active_claim_provider_outage_is_waiting_binding_then_idempotent_recovery() -> None:
    provider = RecordingProvider()
    provider.offline = True
    reconcile = ReconcileChannelBinding(devices=DeviceReader(), provider=provider, clock=Clock())

    assert await reconcile.execute(device_ref=REF) == ()
    provider.offline = False
    first = await reconcile.execute(device_ref=REF)
    second = await reconcile.execute(device_ref=REF)

    assert first == second
    assert len(first) == 1
    assert {call["operation_id"] for call in provider.provisions} == {
        provider.provisions[0]["operation_id"]
    }


@pytest.mark.asyncio
async def test_a_live_binding_is_returned_without_issuing_any_operation() -> None:
    """Reading is not writing. The steady state must mutate nothing."""

    provider = RecordingProvider()
    reconcile = ReconcileChannelBinding(devices=DeviceReader(), provider=provider, clock=Clock())
    await reconcile.execute(device_ref=REF)
    provisions_after_first = len(provider.provisions)

    again = await reconcile.execute(device_ref=REF)

    assert len(again) == 1
    assert len(provider.provisions) == provisions_after_first
    assert provider.refreshes == []


@pytest.mark.asyncio
async def test_an_expired_binding_advances_with_a_refresh_chained_off_it() -> None:
    provider = RecordingProvider()
    provider.seed_expired_binding("channel-provision-seeded")
    reconcile = ReconcileChannelBinding(devices=DeviceReader(), provider=provider, clock=Clock())

    channels = await reconcile.execute(device_ref=REF)

    assert channels[0].expires_at_ms > int(NOW.timestamp() * 1000)
    assert provider.provisions == []
    assert len(provider.refreshes) == 1
    assert provider.refreshes[0]["operation_id"].startswith("channel-refresh-")


@pytest.mark.asyncio
async def test_a_device_keeps_its_channel_across_repeated_credential_expiry() -> None:
    """The defect: a channel could be advanced exactly once, then never again.

    provision -> expiry -> refresh worked, and the refresh fenced the provision
    row. The next reconcile began again at ``provision`` with the same derived
    id and was refused as "a terminal fenced lifecycle" for good. On hardware a
    device lost its channel about two hours after enrolment while its Claim,
    mount and Companion binding all still read healthy, and it sat in
    waiting-binding forever.
    """

    provider = RecordingProvider()
    reconcile = ReconcileChannelBinding(devices=DeviceReader(), provider=provider, clock=Clock())
    first = await reconcile.execute(device_ref=REF)
    assert len(first) == 1

    for round_number in range(4):
        provider.seed_expired_binding(provider._binding.operation_id)
        channels = await reconcile.execute(device_ref=REF)
        assert len(channels) == 1, f"lost its channel on round {round_number}"
        assert channels[0].expires_at_ms > int(NOW.timestamp() * 1000)

    # Advancing is what carried it; the first operation was never re-issued.
    assert len(provider.provisions) == 1
    assert len(provider.refreshes) == 4
    assert len({call["operation_id"] for call in provider.refreshes}) == 4


@pytest.mark.asyncio
async def test_http_adapter_preserves_provider_problem_and_exact_generation() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = json.loads(request.content)
        requests.append(document)
        if request.url.path.endswith("/revoke"):
            return httpx.Response(
                409,
                json={
                    "code": "STALE_GENERATION",
                    "retryable": False,
                    "authority": "eidolon-channel-provider",
                    "detail": "old generation",
                },
            )
        return httpx.Response(
            200,
            json={
                "operation": "channel.provisioned-device",
                "operation_id": document["operation_id"],
                "device_ref": REF.model_dump(mode="json"),
                "manifest_revision": _manifest().digest,
                "channels": [
                    item.model_dump(mode="json")
                    for item in _channel(int((NOW + timedelta(minutes=30)).timestamp() * 1000))
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ChannelProviderHttpClient(client, token="t" * 32)
        device = _device()
        channels = await provider.provision(
            operation_id="channel-provision-01",
            device_ref=REF,
            owner_id="owner_01",
            display_name=device.display_name,
            device_kind=device.device_kind,
            # Verbatim, as the Provider's contract says the Hub forwards it.
            manifest=json.loads(device.manifest_json),
            manifest_revision=device.manifest_digest,
        )
        with pytest.raises(ChannelProviderError) as caught:
            await provider.revoke(
                operation_id="channel-revoke-01",
                device_ref=REF,
                reason="owner-request",
            )

    assert channels[0].channel_id == "channel_01"
    assert requests[0]["device_ref"] == REF.model_dump(mode="json")
    assert caught.value.code == "STALE_GENERATION"
    assert caught.value.retryable is False


async def _seed_revoke(database: HubDatabase) -> None:
    event = {
        "specversion": "1.0",
        "id": "claim_event_channel_7",
        "source": "urn:eidolon:authority:admission",
        "type": "live.eidolon.device.claim-revoked.v1",
        "subject": f"device-instances/{REF.device_instance_id}",
        "time": NOW.isoformat().replace("+00:00", "Z"),
        "datacontenttype": "application/json",
        "dataschema": "https://contracts.eidolon.live/device-foundation/v1/events/claim-revoked-data.schema.json",
        "audience": "eidolon-claim-consumers",
        "ownerdomainid": str(REF.owner_domain_id),
        "aggregaterev": 8,
        "correlationid": "removal_intent_7",
        "causationid": "revoke_claim_7",
        "data": {
            "device_ref": REF.model_dump(mode="json"),
            "reason": "owner-request",
            "revoked_at": NOW.isoformat().replace("+00:00", "Z"),
        },
    }
    async with database.sessions.begin() as session:
        session.add(
            AdmissionClaimEventStreamRow(
                event_id=event["id"],
                owner_domain_id=str(REF.owner_domain_id),
                event_type=event["type"],
                event_json=json.dumps(event, sort_keys=True, separators=(",", ":")),
                occurred_at=NOW,
            )
        )


@pytest.mark.asyncio
async def test_revoke_survives_provider_outage_and_hub_restart_then_is_exactly_once_terminal(
    tmp_path,
) -> None:
    path = tmp_path / "hub.sqlite3"
    database = HubDatabase.sqlite(path)
    await database.initialize_schema()
    await _seed_revoke(database)
    provider = RecordingProvider()
    provider.offline = True
    clock = Clock()
    first = ReconcileChannelRevocations(
        store=SqlChannelRevocationStore(database), provider=provider, clock=clock
    )
    assert await first.execute() == 0
    await database.close()

    clock.value += timedelta(seconds=2)
    restarted = HubDatabase.sqlite(path)
    await restarted.initialize_schema()
    provider.offline = False
    second = ReconcileChannelRevocations(
        store=SqlChannelRevocationStore(restarted), provider=provider, clock=clock
    )
    assert await second.execute() == 1
    assert await second.execute() == 0

    async with restarted.sessions() as session:
        row = await session.scalar(select(ChannelRevocationDeliveryRow))
    assert row is not None
    assert (row.state, row.result_code, row.attempt_count) == ("delivered", "REVOKED", 1)
    assert len(provider.revocations) == 2
    assert {item["operation_id"] for item in provider.revocations} == {
        provider.revocations[0]["operation_id"]
    }
    await restarted.close()


@pytest.mark.asyncio
async def test_stale_revoke_is_terminal_fenced_and_never_touches_new_generation(tmp_path) -> None:
    database = HubDatabase.sqlite(tmp_path / "hub.sqlite3")
    await database.initialize_schema()
    await _seed_revoke(database)
    provider = RecordingProvider()
    provider.stale_revoke = True
    reconcile = ReconcileChannelRevocations(
        store=SqlChannelRevocationStore(database), provider=provider, clock=Clock()
    )

    assert await reconcile.execute() == 1
    async with database.sessions() as session:
        row = await session.scalar(select(ChannelRevocationDeliveryRow))
    assert row is not None
    assert (row.state, row.result_code) == ("fenced", "STALE_GENERATION")
    assert await reconcile.execute() == 0
    await database.close()


async def test_a_manifest_in_the_device_s_own_vocabulary_still_binds() -> None:
    """The Provider is handed the accepted Manifest verbatim, whatever shape it is.

    Parsing it into this Authority's affordance model meant a device whose
    Manifest simply looked different — a real BOX-3 sends `{"endpoints": []}` —
    raised above the guard that answers "binding pending", so the configuration
    pull answered 500 to a correctly claimed device on every boot.
    """

    forwarded: list[object] = []

    class _Provider:
        async def current(self, *, device_ref):
            return None

        async def provision(self, *, manifest, **_values):
            forwarded.append(manifest)
            return (
                ChannelBinding(
                    channel_id="channel_01",
                    purpose="device-session",
                    kinds=("audio",),
                    binding_format="application/vnd.eidolon.livekit-session+json;v=2",
                    issued_at_ms=1,
                    expires_at_ms=1_800_000_000_000,
                    opaque_binding="e30=",
                ),
            )

        async def refresh(self, **_values):
            raise AssertionError("a live binding was not refreshed")

    class _Devices:
        async def get(self, _device_id):
            return replace(_device(), manifest=_canonical_device_manifest())

    channels = await ReconcileChannelBinding(
        devices=_Devices(),
        provider=_Provider(),
        clock=Clock(),
    ).execute(device_ref=REF)

    assert [binding.channel_id for binding in channels] == ["channel_01"]
    assert forwarded == [{"endpoints": []}]
