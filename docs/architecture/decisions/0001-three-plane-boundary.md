# ADR 0001: Separate connection, device management, and channel control

- Status: superseded by ADR 0011
- Date: 2026-08-01

## Decision

This ADR recorded the first separation attempt. ADR 0011 removes the transport
Connection Plane and replaces it with HTTPS Device Sessions plus direct Channel
Acquisition.

LiveKit is not a device connection mechanism. MQTT is a rendezvous transport
and cannot carry operational data.

## Consequences

The existing LiveKit presence and command runtime must be split. Routers call
application use cases rather than obtaining the complete DataStore from
`app.state`. Architecture tests enforce the dependency direction.
