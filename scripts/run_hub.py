#!/usr/bin/env python3
"""Eidolon Hub 启动脚本."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.main import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
