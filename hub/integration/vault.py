"""Encrypted at rest: a Provider account's secrets, keyed by account and name.

AES-256-GCM with a key the operator hands this process (``EIDOLON_HUB_VAULT_KEY``,
32 bytes, base64). The account id and secret name are authenticated data, so a
row cannot be moved to another account by editing the file. Rotation is "bind
the account again"; there is no in-place re-encryption yet.
"""

from __future__ import annotations

import base64
import os
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import IntegrationStore

KEY_ENV = "EIDOLON_HUB_VAULT_KEY"
KEY_BYTES = 32


class VaultKeyError(RuntimeError):
    """The vault key is missing or malformed; the vault refuses to open."""


def load_vault_key(environ=os.environ) -> bytes:
    raw = environ.get(KEY_ENV, "")
    if not raw:
        raise VaultKeyError(f"{KEY_ENV} is not set")
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise VaultKeyError(f"{KEY_ENV} is not base64") from exc
    if len(key) != KEY_BYTES:
        raise VaultKeyError(f"{KEY_ENV} must decode to {KEY_BYTES} bytes")
    return key


def generate_vault_key() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


class CredentialVault:
    def __init__(self, store: IntegrationStore, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise VaultKeyError(f"vault key must be {KEY_BYTES} bytes")
        self._store = store
        self._aead = AESGCM(key)

    async def put(self, account_id: str, name: str, secret: str) -> None:
        nonce = os.urandom(12)
        ciphertext = self._aead.encrypt(nonce, secret.encode(), _aad(account_id, name))
        with self._store.transaction() as db:
            db.execute(
                """INSERT INTO credentials VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (account_id, key) DO UPDATE SET nonce=excluded.nonce,
                ciphertext=excluded.ciphertext, updated_at_ms=excluded.updated_at_ms""",
                (account_id, name, nonce, ciphertext, _now_ms()),
            )

    async def get(self, account_id: str, name: str) -> str | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT nonce, ciphertext FROM credentials WHERE account_id=? AND key=?",
                (account_id, name),
            ).fetchone()
        if row is None:
            return None
        return self._aead.decrypt(row[0], row[1], _aad(account_id, name)).decode()

    async def names(self, account_id: str) -> list[str]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT key FROM credentials WHERE account_id=? ORDER BY key", (account_id,)
            ).fetchall()
        return [row[0] for row in rows]

    async def delete_all(self, account_id: str) -> None:
        with self._store.transaction() as db:
            db.execute("DELETE FROM credentials WHERE account_id=?", (account_id,))


def _aad(account_id: str, name: str) -> bytes:
    return f"{account_id}\x00{name}".encode()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
