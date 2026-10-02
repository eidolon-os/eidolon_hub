"""The integration primitives: encrypted vault, durable receipts, observation stream."""

import asyncio
import base64

import pytest

from hub.integration.accounts import AccountRecord, ProviderAccountStore
from hub.integration.ledger import MemoryLedger, ReceiptConflict, SqliteReceiptLedger, Timestamps
from hub.integration.observation import MemoryObservationCache, SqliteObservationCache
from hub.integration.registry import ProviderRegistry
from hub.integration.store import IntegrationStore
from hub.integration.vault import CredentialVault, VaultKeyError, generate_vault_key, load_vault_key


@pytest.fixture
def store(tmp_path):
    value = IntegrationStore(tmp_path / "integration.sqlite3")
    value.initialize()
    return value


def test_vault_key_is_32_bytes_base64():
    key = load_vault_key({"EIDOLON_HUB_VAULT_KEY": generate_vault_key()})
    assert len(key) == 32
    with pytest.raises(VaultKeyError):
        load_vault_key({})
    with pytest.raises(VaultKeyError):
        load_vault_key({"EIDOLON_HUB_VAULT_KEY": base64.b64encode(b"short").decode()})


async def test_vault_round_trips_and_binds_to_the_account(store):
    vault = CredentialVault(store, load_vault_key({"EIDOLON_HUB_VAULT_KEY": generate_vault_key()}))
    await vault.put("acc1", "token", "s3cret")
    assert await vault.get("acc1", "token") == "s3cret"
    assert await vault.get("acc2", "token") is None
    assert await vault.names("acc1") == ["token"]
    with store.transaction() as db:
        assert b"s3cret" not in db.execute("SELECT ciphertext FROM credentials").fetchone()[0]
        # Moving the row to another account must not decrypt.
        db.execute("UPDATE credentials SET account_id='acc2'")
    with pytest.raises(Exception):
        await vault.get("acc2", "token")
    await vault.delete_all("acc2")
    assert await vault.names("acc2") == []


async def test_accounts_are_owner_scoped(store):
    accounts = ProviderAccountStore(store)
    await accounts.upsert(AccountRecord("a1", "owner-a", "demo", "家", "connected"))
    await accounts.upsert(
        AccountRecord(
            "a2", "owner-b", "demo", "家2", "pending", choices=({"value": "h", "label": "H"},)
        )
    )
    assert [a.account_id for a in await accounts.list("owner-a")] == ["a1"]
    assert (await accounts.get("owner-a", "a2")) is None
    await accounts.set_status("a1", "degraded", error="401", seen=True)
    record = await accounts.get("owner-a", "a1")
    assert record.status == "degraded" and record.error == "401" and record.last_seen_ms
    assert await accounts.delete("owner-a", "a1") is True
    assert await accounts.delete("owner-a", "a1") is False


@pytest.mark.parametrize("factory", ["sqlite", "memory"])
async def test_ledger_repeat_conflict_and_restart(store, factory):
    ledger = SqliteReceiptLedger(store) if factory == "sqlite" else MemoryLedger()
    assert await ledger.begin("o", "", "r1", "fp", 1_000) is None
    await ledger.complete(
        "o", "", "r1", {"ok": True}, Timestamps(1_000, 1_001, 1_050, 1_060, 1_061)
    )
    again = await ledger.begin("o", "", "r1", "fp", 2_000)
    assert again is not None and again.result == {"ok": True}
    assert again.timestamps.provider_returned_at_ms == 1_050
    with pytest.raises(ReceiptConflict):
        await ledger.begin("o", "", "r1", "other", 2_000)
    # In flight: a repeat sees the row without a result.
    assert await ledger.begin("o", "", "r2", "fp", 3_000) is None
    pending = await ledger.begin("o", "", "r2", "fp", 3_001)
    assert pending is not None and pending.result is None
    if factory == "sqlite":
        reopened = SqliteReceiptLedger(IntegrationStore(store.path))
        assert (await reopened.get("o", "", "r1")).result == {"ok": True}


@pytest.mark.parametrize("factory", ["sqlite", "memory"])
async def test_observations_sequence_and_long_poll(store, factory):
    cache = SqliteObservationCache(store) if factory == "sqlite" else MemoryObservationCache()
    first = await cache.put("o", "light", reachable=True, state={"on": True}, observed_at_ms=1)
    assert first.seq == 1
    assert (
        await cache.put("o", "light", reachable=True, state={"on": True}, observed_at_ms=2) is None
    )
    assert (await cache.snapshot("o"))["light"].observed_at_ms == 2
    assert await cache.changes_since("o", 1, timeout_s=0.05) == []

    async def later():
        await asyncio.sleep(0.05)
        await cache.put("o", "light", reachable=False, state={"on": True}, observed_at_ms=3)

    waiter = asyncio.create_task(cache.changes_since("o", 1, timeout_s=2))
    await later()
    changes = await waiter
    assert [c.seq for c in changes] == [2] and changes[0].reachable is False
    assert await cache.latest_seq("o") == 2
    await cache.forget("o", {"light"})
    assert await cache.snapshot("o") == {}


def test_registry_builds_only_known_kinds():
    registry = ProviderRegistry()
    registry.register("virtual", lambda: "V")
    assert registry.build(["virtual", "virtual"]) == {"virtual": "V"}
    with pytest.raises(ValueError, match="unknown smart-home provider kind"):
        registry.build(["virtual", "nope"])
    with pytest.raises(ValueError):
        registry.register("virtual", lambda: "again")
