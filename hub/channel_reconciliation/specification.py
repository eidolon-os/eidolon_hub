"""The existing Channel device wire shape, shared by reconciliation and visits."""

from collections.abc import Mapping


def channel_device_payload(
    *,
    owner_id: str,
    display_name: str,
    manifest_id: str,
    manifest: Mapping[str, object],
    manifest_revision: str,
    output_policy=None,
) -> dict:
    return {
        "owner_id": owner_id,
        "display_name": display_name,
        "device_kind": manifest_id,
        "manifest": dict(manifest),
        "manifest_revision": manifest_revision,
        **({"output_policy": output_policy.model_dump(mode="json")} if output_policy else {}),
    }
