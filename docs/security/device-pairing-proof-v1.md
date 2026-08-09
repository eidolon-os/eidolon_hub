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

## Security boundary

This is proof of access to the device-generated secret plus continuity of its
TOFU P-256 key; it is not manufacturer attestation. An attacker who can read the
device display/local pairing transport, extract flash secrets, compromise Hub
TLS, or replace the device before first enrollment can defeat or deny pairing.
Production firmware therefore needs secure boot, flash/NVS encryption, verified
Hub TLS, CSPRNG output, bounded enrollment expiry, secret redaction, and a local
pairing channel appropriate to the product threat model.
