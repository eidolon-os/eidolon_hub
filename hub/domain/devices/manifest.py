"""Protocol-neutral device-manifest document."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class DeviceManifestDocument:
    """One accepted account of what a device can do, and when it said so.

    Two different things used to share the name ``revision``: the digest of the
    content, which identifies *which* document this is, and the device's own
    count of how many times its capabilities have changed, which orders its
    successive accounts of itself. They are kept apart here because only the
    second can decide whether an incoming assertion is newer.
    """

    canonical_json: str = field(repr=False)
    digest: str
    declared_revision: int

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
        if self.declared_revision < 1:
            raise ValueError("device manifest declared_revision must be positive")
        expected = "sha256:" + hashlib.sha256(self.canonical_json.encode()).hexdigest()
        if self.digest != expected:
            raise ValueError("device manifest digest does not match canonical content")

    @classmethod
    def from_declaration(
        cls, *, document: Mapping[str, Any], declared_revision: int
    ) -> "DeviceManifestDocument":
        """Adopt a document a device has declared, at the revision it declared."""

        if not isinstance(document, Mapping):
            raise ValueError("device manifest must be an object")
        canonical = json.dumps(
            dict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            canonical_json=canonical,
            digest="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
            declared_revision=declared_revision,
        )
