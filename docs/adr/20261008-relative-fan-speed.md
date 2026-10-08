# Relative fan-speed commands

The SDK adds `fan_speed.step` with integer `delta` in the existing -100..100 step range. VirtualProvider reads and updates persisted speed within its SQLite transaction, clamps to 0..100, and returns actual state. Power state is preserved, matching fan_speed.set. Repeated unique requests each apply a step; duplicate request IDs use the runtime receipt and must not double-apply, including after restart with a durable ledger.

Home Assistant maps a step to set_percentage using the current cached speed. Unknown, non-integer or out-of-range speed is refused rather than assumed zero. Effect confirmation checks the expected clamped value, including a no-op at the limit. The external Home Assistant read/modify/write is not an atomic transaction with third-party controllers; concurrent external changes remain an integration limitation.

Automated coverage includes positive/negative steps, limits, restart, external absolute changes, missing-state refusal, exact effect receipts, concurrent retries and durable idempotency. Live Home Assistant bench tests require its separately provisioned bench and are not part of the Mac-only isolated gate.
