# ADR 0002: Hub owns its contracts and removes the direct SDK dependency

- Status: accepted
- Date: 2026-08-01

## Decision

Hub-owned device access, device management, session, manifest and channel-control
contracts live in this repository. Hub source, tests, and project metadata do
not directly depend on `eidolon_sdk`.

Hub also owns its device-control runtime schema. `eidolon_data` remains
unchanged as a sibling data-sovereignty service, but it is no longer imported,
installed or opened by Hub. Hub's SQLite adapter implements its repository
ports directly. Consequently `eidolon_sdk` is absent from both the direct and
transitive Hub dependency tree. PostgreSQL support was later removed by ADR
0015 without changing this ownership decision.

LiveKit token construction is not migrated into Hub's new contract package. It
belongs to the external channel provider.
