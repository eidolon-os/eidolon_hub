# Channel binding readiness

An active Claim authorizes configuration reads; it does not imply that an Owner
has selected input/output permissions. Admission ACK must not silently create
an output policy.

`configuration:pull` continues to return HTTP 200 for an active Claim when no
Channel is available. Its additive `channel_problem` field explains why:

- `null`: no refusal is reported.
- `OUTPUT_POLICY_REQUIRED`, `retryable=false`: ask the Owner to select permissions.
- A Provider error with its retryability and detail: distinguish an outage from
  an unchanged request that the Provider cannot accept.

Hub uses the shared presentation contract to recognize the missing Owner
policy before calling Provider. This does not change the Provider's own
validation. Saving a policy still invokes the existing reconciliation callback;
the next configuration pull can carry the binding without re-enrollment.

Other non-retryable Provider refusals are memoized by DeviceRef, Manifest digest
and declared revision, Owner, policy, and observed connection address. This is
bounded process-local suppression (up to 1024 devices), not lifecycle authority
or a database migration. Changed inputs, eviction, or a process restart allow a
fresh decision. Retryable outages are not memoized.

Devices may continue reading configuration to discover an Owner decision.
This is distinct from retrying the same rejected provisioning request. Older
clients can ignore the additive field; updated clients show the missing policy
as an Owner action rather than indefinite service preparation.
