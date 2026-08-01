from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import aiomqtt
import pytest
from sqlalchemy import delete

from hub.adapters.connections.mqtt import Mqtt5Connector
from hub.adapters.persistence.database import HubDatabase
from hub.adapters.persistence.models import ChannelProviderSyncRow, DeviceRow
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.domain.channels.entities import ProviderSyncRecord, ProviderSyncState
from hub.domain.devices.entities import ManagedDevice
from hub.domain.devices.identity import DeviceIdentity
from hub.domain.devices.manifest import DeviceManifestDocument

POSTGRES_DSN = os.environ.get("EIDOLON_HUB_TEST_POSTGRES_DSN", "").strip()
MQTT_HOST = os.environ.get("EIDOLON_HUB_TEST_MQTT_HOST", "").strip()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="requires dedicated EIDOLON_HUB_TEST_POSTGRES_DSN",
)
async def test_real_postgresql_adapter_round_trip() -> None:
    database = HubDatabase.postgresql(POSTGRES_DSN)
    await database.init_schema()
    device_id = f"cloud-functional-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    device = ManagedDevice(
        identity=DeviceIdentity(device_id, "p256:cloud-functional", "cloud-test"),
        display_name="Cloud Functional Device",
        device_kind="reference-device",
        manifest=DeviceManifestDocument.from_mapping(
            {"schema_version": 1, "title": "Cloud Functional Device"}
        ),
        registered_at=now,
        updated_at=now,
    )
    try:
        repository = SqlHubRepositories(database).devices
        await repository.upsert(device)
        assert await repository.get(device_id) == device
    finally:
        async with database.sessions.begin() as session:
            await session.execute(delete(DeviceRow).where(DeviceRow.device_id == device_id))
        await database.close()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="requires dedicated EIDOLON_HUB_TEST_POSTGRES_DSN",
)
async def test_real_postgresql_concurrent_channel_sync_claim() -> None:
    first_database = HubDatabase.postgresql(POSTGRES_DSN)
    second_database = HubDatabase.postgresql(POSTGRES_DSN)
    await first_database.init_schema()
    device_id = f"cloud-claim-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    desired = ProviderSyncRecord(
        device_id=device_id,
        operation_id=f"channel-sync:sha256:{uuid.uuid4().hex}",
        desired_revision=f"sha256:{uuid.uuid4().hex}",
        state=ProviderSyncState.PENDING,
        attempts=0,
        updated_at=now,
    )
    first_repository = SqlHubRepositories(first_database).channel_provider_sync
    second_repository = SqlHubRepositories(second_database).channel_provider_sync
    try:
        claims = await asyncio.gather(
            first_repository.try_claim(
                desired,
                owner_instance_id="cloud-hub-1",
                now=now,
                claim_ttl=timedelta(seconds=30),
            ),
            second_repository.try_claim(
                desired,
                owner_instance_id="cloud-hub-2",
                now=now,
                claim_ttl=timedelta(seconds=30),
            ),
        )

        assert sum(claim is not None for claim in claims) == 1
        assert {claim.owner_instance_id for claim in claims if claim is not None} <= {
            "cloud-hub-1",
            "cloud-hub-2",
        }
    finally:
        async with first_database.sessions.begin() as session:
            await session.execute(
                delete(ChannelProviderSyncRow).where(ChannelProviderSyncRow.device_id == device_id)
            )
        await second_database.close()
        await first_database.close()


@pytest.mark.skipif(
    not MQTT_HOST,
    reason="requires EIDOLON_HUB_TEST_MQTT_HOST and a disposable MQTT5 broker",
)
async def test_real_mqtt5_connector_request_reply() -> None:
    port = int(os.environ.get("EIDOLON_HUB_TEST_MQTT_PORT", "1883"))
    username = os.environ.get("EIDOLON_HUB_TEST_MQTT_USERNAME") or None
    password = os.environ.get("EIDOLON_HUB_TEST_MQTT_PASSWORD") or None
    device_id = f"cloudmqtt-{uuid.uuid4().hex}"

    async def handler(message):
        return json.dumps(
            {
                "operation": "connection.challenge",
                "request_id": message.contract.request_id,
                "challenge_id": "cloud-challenge-1",
                "server_nonce": "cloud-server-nonce-0001",
                "expires_at_ms": int(
                    (datetime.now(UTC) + timedelta(seconds=30)).timestamp() * 1000
                ),
            }
        ).encode()

    connector = Mqtt5Connector(
        connector_id="mqtt-cloud-functional",
        hostname=MQTT_HOST,
        port=port,
        username=username,
        password=password,
        handler=handler,
        reconnect_seconds=0.1,
        client_id=f"hub-functional-{uuid.uuid4().hex}",
    )
    await connector.start()
    try:
        async with aiomqtt.Client(
            MQTT_HOST,
            port,
            username=username,
            password=password,
            identifier=f"device-functional-{uuid.uuid4().hex}",
            protocol=aiomqtt.ProtocolVersion.V5,
        ) as client:
            outbound = f"eidolon/v1/devices/{device_id}/connection/out"
            await client.subscribe(outbound, qos=1)
            await asyncio.sleep(0.2)
            await client.publish(
                f"eidolon/v1/devices/{device_id}/connection/in",
                json.dumps(
                    {
                        "operation": "connection.hello",
                        "request_id": "cloud-hello-1",
                        "device_id": device_id,
                        "connector_id": "mqtt-cloud-functional",
                        "client_nonce": "cloud-client-nonce-0001",
                    }
                ).encode(),
                qos=1,
            )
            async with asyncio.timeout(10):
                async for message in client.messages:
                    response = json.loads(bytes(message.payload))
                    assert response["operation"] == "connection.challenge"
                    break
    finally:
        await connector.stop()
