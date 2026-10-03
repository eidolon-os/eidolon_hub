"""Home Assistant as the ecosystem adapter: one WebSocket per bound instance.

Confirmation style: event-confirming. ``call_service`` returning is not the
device having changed (a cloud-pushed integration such as 米家 reports the new
state only when its cloud does), so ``execute`` returns after the entity's
state satisfies the command, or raises ``TimeoutError``. Everything Home
Assistant specific — entity ids, domains, service names, attribute names —
stays in this package; the SDK sees traits and state keys only.
"""
