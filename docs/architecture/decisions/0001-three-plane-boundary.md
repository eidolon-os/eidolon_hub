# ADR 0001: Separate connection, device management, and channel control

- Status: accepted
- Date: 2026-08-01

## Decision

Hub uses independent connection, device-management, and channel-control
boundaries. Connection adapters emit normalized events and do not persist
device facts. Channel providers are invoked through a provider-neutral port and
own all provider-specific connection details.

LiveKit is not a device connection mechanism. MQTT is a rendezvous transport
and cannot carry operational data.

## Consequences

The existing LiveKit presence and command runtime must be split. Routers call
application use cases rather than obtaining the complete DataStore from
`app.state`. Architecture tests enforce the dependency direction.
