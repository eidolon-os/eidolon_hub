"""Server-Sent Events framing owned by the HTTP interface."""

from __future__ import annotations

import json
from typing import Any

SSE_HEARTBEAT_COMMENT = "keepalive"
SSE_HEARTBEAT_BYTES = b": keepalive\n\n"


def encode_sse_comment(comment: str = SSE_HEARTBEAT_COMMENT) -> bytes:
    return f": {comment}\n\n".encode("utf-8")


def encode_sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
