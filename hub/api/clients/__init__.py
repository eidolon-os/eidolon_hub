"""HTTP clients for outbound calls from hub.

Phase 32.A introduced :mod:`hub.api.clients.admin` — a thin wrapper over
the eidolon-admin gateway's user lookup, used by ``/api/config`` to
verify the requested ``user_id`` exists before minting a LiveKit token
(Phase 29.H — 杜绝陌生 user_id 在入口拦下).
"""

from hub.api.clients.admin import (
    AdminClient,
    AdminClientError,
    AdminNotFound,
    AdminUnreachable,
    AdminUpstreamError,
)

__all__ = [
    "AdminClient",
    "AdminClientError",
    "AdminNotFound",
    "AdminUnreachable",
    "AdminUpstreamError",
]
