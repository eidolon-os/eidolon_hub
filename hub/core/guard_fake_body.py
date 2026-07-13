"""Fake StackChan body endpoint helpers for Guard control-plane tests."""

from __future__ import annotations

from typing import Any, Literal

from eidolon_sdk.biz.body import BODY_OP_PRESENCE_SET

FakeBodyResultStatus = Literal["completed", "failed"]

_PAYLOAD_KEYS = {"state", "guard_epoch", "correlation_id", "action_id"}
_FORBIDDEN_KEYS = {
    "audio",
    "candidate",
    "confidence",
    "embedding",
    "face_score",
    "guard.policy.action",
    "image",
    "media",
    "owner_face",
    "owner_face_match",
    "owner_face_score",
    "presence",
    "raw_image",
    "score",
    "template",
    "verified",
    "verdict",
}
_FORBIDDEN_VALUES = {
    "guard.policy.action",
    "guard.presence.absent",
    "guard.presence.candidate",
    "guard.presence.verified",
    "candidate",
    "verified",
}
_FORBIDDEN_COMPACT_KEYS = {key.replace("_", "").replace(".", "") for key in _FORBIDDEN_KEYS}


def build_fake_body_presence_result(
    envelope: dict[str, Any],
    *,
    sender_identity: str,
    status: FakeBodyResultStatus = "completed",
    action_id_override: str | None = None,
) -> dict[str, Any]:
    """Validate a body.presence.set command and synthesize a standard result."""

    device_id = sender_identity.strip()
    if not device_id:
        raise ValueError("sender_identity is required")
    _reject_forbidden_content(envelope)
    if envelope.get("v") != 1:
        raise ValueError("fake body command must use protocol v=1")
    if envelope.get("kind") != "cmd":
        raise ValueError("fake body endpoint only accepts command envelopes")
    if envelope.get("op") != BODY_OP_PRESENCE_SET:
        raise ValueError("fake body endpoint only accepts body.presence.set")
    command_id = envelope.get("id")
    if not isinstance(command_id, str) or not command_id:
        raise ValueError("fake body command id is required")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("body.presence.set payload must be an object")
    _validate_body_presence_payload(payload)
    result_action_id = action_id_override or payload["action_id"]
    if not isinstance(result_action_id, str) or not result_action_id:
        raise ValueError("result action_id must be a non-empty string")
    return {
        "v": 1,
        "kind": "result",
        "ref": command_id,
        "device_id": device_id,
        "op": BODY_OP_PRESENCE_SET,
        "status": status,
        "code": "OK" if status == "completed" else "FAKE_BODY_FAILED",
        "result": {
            "action_id": result_action_id,
            "state": payload["state"],
            "applied": status == "completed",
        },
    }


def _validate_body_presence_payload(payload: dict[str, Any]) -> None:
    keys = set(payload)
    if keys != _PAYLOAD_KEYS:
        extra = sorted(keys - _PAYLOAD_KEYS)
        missing = sorted(_PAYLOAD_KEYS - keys)
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        raise ValueError("invalid body.presence.set payload keys: " + " ".join(details))
    if not isinstance(payload["state"], str) or not payload["state"]:
        raise ValueError("body.presence.set state must be a non-empty string")
    guard_epoch = payload["guard_epoch"]
    if isinstance(guard_epoch, bool) or not isinstance(guard_epoch, int):
        raise ValueError("body.presence.set guard_epoch must be an integer")
    if not isinstance(payload["correlation_id"], str) or not payload["correlation_id"]:
        raise ValueError("body.presence.set correlation_id must be a non-empty string")
    if not isinstance(payload["action_id"], str) or not payload["action_id"]:
        raise ValueError("body.presence.set action_id must be a non-empty string")


def _reject_forbidden_content(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _forbidden_token(key):
                raise ValueError(f"fake body endpoint does not accept guard/media field: {key}")
            _reject_forbidden_content(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_forbidden_content(item)
        return
    if isinstance(value, str) and value in _FORBIDDEN_VALUES:
        raise ValueError(f"fake body endpoint does not accept guard/media value: {value}")


def _forbidden_token(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    compact = normalized.replace("_", "").replace(".", "")
    return normalized in _FORBIDDEN_KEYS or compact in _FORBIDDEN_COMPACT_KEYS
