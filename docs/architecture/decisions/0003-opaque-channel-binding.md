# ADR 0003: Provider-specific channel bindings are opaque

- Status: accepted
- Date: 2026-08-01

## Decision

Hub selects a logical channel profile and calls a generic provisioner. The
provisioner returns a `ChannelGrant` containing a generic lease and opaque bytes.
Hub relays those bytes but does not parse, persist, index, or log them.

Provider configuration owns WSS and LiveKit endpoints, rooms, tokens, TURN
credentials, codecs, and regional placement. Configuration changes affect new
and renewed grants only.
