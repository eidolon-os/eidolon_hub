# ADR 0003: Provider-specific channel bindings are opaque

- Status: revised by ADR 0017
- Date: 2026-08-01

## Decision

The current implementation no longer selects a logical profile or persists a
grant. For an active registration retry, Hub calls the single configured
Provider and relays each generic assignment's opaque bytes in the same response.
Hub does not parse, persist, index, or log them.

Provider configuration owns WSS and LiveKit endpoints, rooms, tokens, TURN
credentials, codecs, and regional placement. Configuration changes affect
future Provider provisions only. See ADR 0017 for the request-scoped decision.
