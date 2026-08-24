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
    _capability_names: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        try:
            value = json.loads(self.canonical_json)
        except json.JSONDecodeError as exc:
            raise ValueError("device manifest must contain valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("device manifest must be a JSON object")
        # `schema_version` is what a manifest authored against *this* vocabulary
        # declares, and it is checked when one is. It is not required, because
        # this type also reads manifests it did not author: a device's accepted
        # canonical Manifest is an opaque document to the Authority, and the
        # owner-facing directory is a projection of it.
        #
        # Requiring it made a projection row able to kill the Authority. The
        # first device ever claimed canonically sent `{"endpoints":[]}`, the
        # directory hydrated every row at startup, and Hub crash-looped on boot
        # — admitting nothing, answering nothing, for a document it had already
        # accepted.
        if "schema_version" in value and value["schema_version"] != 1:
            raise ValueError("device manifest schema_version 1 is required")
        expected = "sha256:" + hashlib.sha256(self.canonical_json.encode()).hexdigest()
        if self.revision != expected:
            raise ValueError("device manifest revision does not match canonical content")
        names = {
            item["name"]
            for collection in ("properties", "actions", "events")
            for item in value.get(collection, ())
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
        }
        names.update(
            item["kind"]
            for item in value.get("media", ())
            if isinstance(item, dict) and isinstance(item.get("kind"), str) and item["kind"]
        )
        object.__setattr__(self, "_capability_names", frozenset(names))

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

    def declares_capability(self, name: str) -> bool:
        """Match a declared property, action, event or media kind by exact name."""

        return name in self._capability_names
