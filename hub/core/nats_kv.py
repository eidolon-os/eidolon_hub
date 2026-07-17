"""Minimal JetStream KV adapter owned by Hub."""

from __future__ import annotations

import nats
import nats.js.errors
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.api import KeyValueConfig
from nats.js.kv import KeyValue


class HubNatsKVBucket:
    def __init__(
        self,
        nc: NatsClient,
        js: JetStreamContext,
        kv: KeyValue,
        bucket: str,
    ) -> None:
        self._nc = nc
        self._js = js
        self._kv = kv
        self._bucket = bucket

    @classmethod
    async def connect(
        cls,
        *,
        url: str,
        bucket: str,
        creds_path: str | None = None,
    ) -> "HubNatsKVBucket":
        nc = await nats.connect(
            url,
            user_credentials=creds_path,
            name="eidolon-hub-device-blackboard",
            connect_timeout=5,
            max_reconnect_attempts=-1,
        )
        js = nc.jetstream()
        try:
            kv = await js.key_value(bucket)
        except nats.js.errors.BucketNotFoundError:
            kv = await js.create_key_value(
                KeyValueConfig(bucket=bucket, history=1)
            )
        return cls(nc, js, kv, bucket)

    async def clear(self) -> None:
        await self._js.purge_stream(f"KV_{self._bucket}")

    async def get(self, key: str) -> bytes | None:
        try:
            return (await self._kv.get(key)).value
        except nats.js.errors.KeyNotFoundError:
            return None

    async def put(self, key: str, value: bytes) -> int:
        return await self._kv.put(key, value)

    async def delete(self, key: str) -> None:
        await self._kv.purge(key)

    async def keys(self, prefix: str = "") -> list[str]:
        try:
            keys = await self._kv.keys()
        except nats.js.errors.NoKeysError:
            return []
        return sorted(key for key in keys if key.startswith(prefix))

    async def close(self) -> None:
        await self._nc.drain()


__all__ = ["HubNatsKVBucket"]
