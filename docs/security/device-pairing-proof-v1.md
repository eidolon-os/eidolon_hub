# Device pairing proof v1

This contract separates network onboarding from Owner admission. A device may be
connected to Wi-Fi and enrolled as `pending-approval` without belonging to any
Owner. `device_id`, mDNS, and the enrollment retrieval token are not Owner
proofs.

## Device enrollment input

The device generates and persists three independent values before its first
`POST /api/device-onboarding/v1/enrollments` attempt:

- a P-256 private key (retained across firmware restarts),
- a 256-bit retrieval token (retained through handoff), and
- a 256-bit pairing secret (retained until pairing expires or succeeds).

The enrollment adds:

```json
{
  "identity_proof": {
    "algorithm": "p256-sha256",
    "public_key_spki": "<base64url DER SubjectPublicKeyInfo>",
    "signature": "<base64url DER ECDSA signature>"
  },
  "pairing_proof": {
    "method": "local-secret-sha256",
    "commitment": "sha256:<lowercase SHA-256 of the UTF-8 pairing secret>"
  }
}
```

The ECDSA signature covers this length-prefixed UTF-8 statement:

```text
eidolon-device-enrollment-proof-v1\n
<byte-length>:<request_id>\n
<byte-length>:<device_id>\n
<byte-length>:sha256:<retrieval-token-sha256>\n
<byte-length>:<pairing method or empty>\n
<byte-length>:<pairing commitment or empty>\n
<byte-length>:<device_kind>\n
<byte-length>:<display_name>\n
<byte-length>:<canonical manifest revision>\n
```

Canonical manifest revision is `sha256:` plus SHA-256 of the binding's
sorted-key, compact UTF-8 manifest JSON. Hub verifies P-256 possession and
persists only the SPKI fingerprint and pairing commitment. A restarted pending
enrollment may retain that device ID only with the same P-256 key.

These values are stored in the Hub-owned `hub_devices` row; the plaintext
pairing secret is never persisted by Hub. The repository intentionally has no
in-place database migrations, so a database created against an older ORM schema
is rejected by the existing current-schema check rather than silently altered.

The enrollment receipt returns a concrete HTTPS `pairing_claim_uri` when the
commitment is present. The device's local/near-field pairing payload for a
Mobile or Local Controller is exactly:

```json
{
  "schema_version": 1,
  "hub_id": "<descriptor hub_id>",
  "enrollment_id": "<receipt enrollment_id>",
  "device_id": "<stable device_id>",
  "pairing_claim_uri": "<receipt pairing_claim_uri>",
  "pairing_secret": "<device-generated base64url secret>",
  "expires_at_ms": 1786000000000
}
```

The transport of this payload is deliberately out of Hub scope. Product
implementations must use a physical display/QR, authenticated BLE, authenticated
SoftAP, or another channel that demonstrates local access. Publishing it through
mDNS, Hub logs, or an unauthenticated LAN endpoint destroys the proof.

The ESP display QR profile is a compact transport encoding of two fields from
that object:

```text
EIDOLON:PAIR:1:<enrollment_id>:<pairing_secret>
```

The payload is ASCII, each variable field matches `[A-Za-z0-9_-]+`, and total
length is at most 106 bytes. Current Hub enrollment IDs (`enrollment_` plus 144
random bits encoded base64url) leave room for the ESP's 256-bit base64url secret.
The QR deliberately omits network location: a Controller must use the Hub origin
from its already-verified provisioning/discovery target and address
`/api/device-management/v1/enrollments/{enrollment_id}/pairing-claims`. No URI,
Owner ID, Device ID or lifecycle claim from a QR is trusted. The Hub claim
response supplies the authoritative Device and Owner binding.

## Owner claim input and output

The authenticated Controller posts the local secret to the receipt URI:

```http
POST /api/device-management/v1/enrollments/{enrollment_id}/pairing-claims
Authorization: Bearer <Owner-scoped management JWT>
Content-Type: application/json
```

```json
{
  "operation": "device.pairing-claim",
  "request_id": "<Controller idempotency key>",
  "pairing_secret": "<the local pairing secret>"
}
```

The JWT must contain a verified `sub`, `device-manager` role, and non-empty
`owner_id`. Hub derives the Owner from the signed JWT; neither Owner nor
Controller identity is accepted from the request body. `sub` becomes the audit
principal. On success Hub returns:

```json
{
  "operation": "device.lifecycle-status",
  "device_id": "<enrolled device>",
  "owner_id": "<JWT owner_id>",
  "lifecycle_state": "approved"
}
```

The same request ID and content are idempotent. A different secret, Owner,
principal, or approval method under the same request ID conflicts. A pairing
secret is accepted only for its exact, unexpired pending enrollment. Once bound,
another Owner cannot reuse it. Administrative approval remains a separate
`hub-admin` path for deployments that have no local pairing channel.

HTTP outcomes are part of the contract: `200` returns the lifecycle status;
`403` covers an invalid/consumed proof or missing Owner scope; `404` means the
enrollment is unknown; `409` covers expiry or an idempotency-content conflict;
and schema validation failures return `422`. The exact successful request may be
replayed after approval, including after the pending deadline, because it cannot
change the already-bound Owner.

## Controller and Local API boundary

Mobile does not mint Hub authority and must not send an `owner_id` in the Hub
claim body. The intended Controller-authenticated Local API input is this exact
consumer envelope (this is not a Hub route):

```http
PUT /api/local/v1/device-admissions/{setup_id}
Authorization: Bearer <Controller session>
Content-Type: application/json
```

```json
{
  "contract_version": "1",
  "request_id": "<stable Controller idempotency key>",
  "hub_id": "<verified provisioning target Hub ID>",
  "descriptor_uri": "<verified HTTPS Hub descriptor URI>",
  "enrollment_id": "<physical pairing payload enrollment ID>",
  "pairing_secret": "<physical pairing payload secret>",
  "companion_id": "<optional Owner-scoped Companion>"
}
```

Local API derives `owner_id` and Controller principal from the verified session,
resolves the Hub from the pinned descriptor target, mints/obtains a short-lived
Hub JWT with `aud=eidolon-hub`, `sub=<Controller principal>`,
`roles=[device-manager]`, `owner_id=<authenticated Owner>`, and calls the Hub
pairing endpoint with the same stable `request_id`. It must redact and avoid
checkpointing `pairing_secret`.

The Local API result shape is:

```json
{
  "operation": "local.device-admission-progress",
  "contract_version": "1",
  "setup_id": "<same Local/App workflow ID>",
  "request_id": "<same idempotency key>",
  "device_id": "<Hub response device ID>",
  "enrollment_id": "<same enrollment ID>",
  "owner_id": "<authenticated Owner>",
  "state": "approved|binding|ready|failed",
  "completed_stage": "hub-approved|kernel-mounted|companion-attached",
  "companion_id": "<attached Companion or null>",
  "retryable": false
}
```

`setup_id` is Local/App workflow identity and never crosses into firmware or
Hub. `enrollment_id` is Hub enrollment identity. `request_id` is stable across a
lost response, process restart and forward retry. After Hub approval, Local API
mounts the authoritative returned `device_id` into the authenticated Owner's
Kernel scope; optional `companion_id` is validated inside that Owner scope and
attached only after mount. Hub never accepts or stores `companion_id`. A Hub
success followed by Kernel/Companion failure is a forward-retry state, not a
reason to revoke or re-enroll the device.

The current Hub pairing endpoint implements the Hub half of this boundary. The
Mobile/Local API consumer route and Kernel/Companion orchestration are separate
tasks and are not implemented in this repository.

## Security boundary

mDNS is discovery, not a TLS trust mechanism. `descriptor_uri` and
`enrollment_uri` may therefore use a publicly trusted DNS hostname even though
the service is announced on the local link. Devices must validate the HTTPS
certificate for the URI hostname. A `.local` deployment is valid only when its
private CA has been installed through an authenticated provisioning channel;
disabling certificate or hostname verification is not part of this contract.

This is proof of access to the device-generated secret plus continuity of its
TOFU P-256 key; it is not manufacturer attestation. An attacker who can read the
device display/local pairing transport, extract flash secrets, compromise Hub
TLS, or replace the device before first enrollment can defeat or deny pairing.
Production firmware therefore needs secure boot, flash/NVS encryption, verified
Hub TLS, CSPRNG output, bounded enrollment expiry, secret redaction, and a local
pairing channel appropriate to the product threat model.
