"""Hash and verify high-entropy, device-generated retrieval tokens."""

from __future__ import annotations

import hashlib
import hmac


class Sha256RetrievalTokenHasher:
    """Fast hashing is safe here because contracts require random 32-byte tokens."""

    def hash(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def verify(self, token: str, token_hash: str) -> bool:
        return hmac.compare_digest(self.hash(token), token_hash)
