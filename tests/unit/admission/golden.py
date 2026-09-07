"""The published device-foundation vectors, read from the installed SDK.

A vector copied into this repository would be a second copy of the very thing
every implementation is held to, and the copy is what would drift. These are
read out of the SDK that ships them, so a vector this Hub disagrees with is a
disagreement with the contract rather than with a literal beside the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import eidolon_sdk

GOLDEN = Path(eidolon_sdk.__file__).resolve().parents[1] / "contracts/device_foundation/v1/golden"


def golden_vector(name: str) -> dict:
    path = GOLDEN / name
    assert path.exists(), f"the canonical vector is not at {path}"
    return json.loads(path.read_text(encoding="utf-8"))
