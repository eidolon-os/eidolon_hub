import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PairingSession:
    session_token: str
    pairing_code: str
    created_at: float = field(default_factory=time.time)
    device_id: Optional[str] = None
    device_secret: Optional[str] = None
    device_name: str = ""
    attempts: int = 0

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > 1800

    @property
    def bound(self) -> bool:
        return self.device_id is not None


class PairingStore:
    def __init__(self):
        self._sessions: dict[str, PairingSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("PairingStore started")

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            async with self._lock:
                expired = [k for k, s in self._sessions.items() if s.expired]
                for k in expired:
                    del self._sessions[k]
                if expired:
                    logger.info("Cleaned up %d expired pairing sessions", len(expired))

    async def create(self) -> PairingSession:
        session_token = secrets.token_urlsafe(32)
        pairing_code = "".join(
            str(secrets.randbelow(10)) for _ in range(6)
        )
        session = PairingSession(
            session_token=session_token,
            pairing_code=pairing_code,
        )
        async with self._lock:
            self._sessions[session_token] = session
        logger.info("Created pairing session: code=%s", pairing_code)
        return session

    async def get(self, session_token: str) -> Optional[PairingSession]:
        async with self._lock:
            session = self._sessions.get(session_token)
            if session is None:
                return None
            if session.expired:
                del self._sessions[session_token]
                return None
            return session

    async def bind(
        self,
        session_token: str,
        device_id: str,
        device_secret: str,
        device_name: str = "",
    ) -> bool:
        async with self._lock:
            session = self._sessions.get(session_token)
            if session is None or session.expired:
                return False
            session.device_id = device_id
            session.device_secret = device_secret
            session.device_name = device_name
        logger.info("Pairing session %s bound to device %s", session_token, device_id)
        return True

    async def record_attempt(self, session_token: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_token)
            if session:
                session.attempts += 1
