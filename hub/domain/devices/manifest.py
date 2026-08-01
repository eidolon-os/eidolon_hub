"""Protocol-neutral immutable device-manifest document."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DeviceManifestDocument:
    """Canonical WoT-style manifest retained without a wire-model dependency."""

    canonical_json: str = field(repr=False)
    revision: str

    def __post_init__(self) -> None:
        try:
            value = json.loads(self.canonical_json)
        except json.JSONDecodeError as exc:
            raise ValueError("device manifest must contain valid JSON") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("device manifest schema_version 1 is required")
        expected = "sha256:" + hashlib.sha256(self.canonical_json.encode()).hexdigest()
        if self.revision != expected:
            raise ValueError("device manifest revision does not match canonical content")

    @classmethod
    def from_mapping(cls, value: object) -> "DeviceManifestDocument":
        if not isinstance(value, dict):
            raise ValueError("device manifest must be an object")
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        return cls(canonical_json=canonical, revision=revision)
