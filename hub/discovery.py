"""Run the existing discovery publisher independently of Hub database startup.

Deployment selects one publisher owner for both processes. Discovery announces
an address, never readiness or authority; TLS and signed directories still
authenticate the Host, and ingress returns 503 while Hub is unavailable.
"""

import asyncio
import signal

from hub.composition.device_onboarding import build_mdns_advertiser, discovery_owner
from hub.config import load_hub_config


async def run() -> None:
    if discovery_owner() != "standalone":
        raise RuntimeError("Standalone discovery requires exclusive publisher ownership")
    advertiser = build_mdns_advertiser(load_hub_config())
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        if advertiser is not None:
            await advertiser.start()
        await stopped.wait()
    finally:
        if advertiser is not None:
            await advertiser.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    asyncio.run(run())
