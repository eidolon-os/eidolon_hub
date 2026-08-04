"""Canonical fingerprints for content-aware idempotent mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def mutation_fingerprint(operation: str, values: Mapping[str, str]) -> str:
    """Hash an unambiguous canonical operation document."""

    document = {"operation": operation, "values": dict(values)}
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
