"""Dedicated database migration command for cloud deployment jobs."""

from __future__ import annotations

import asyncio

from hub.composition.resources import create_database
from hub.config import HubConfig, load_hub_config


async def migrate(config: HubConfig) -> None:
    database = create_database(config)
    try:
        await database.migrate()
    finally:
        await database.close()


def main() -> None:
    asyncio.run(migrate(load_hub_config()))


if __name__ == "__main__":
    main()
