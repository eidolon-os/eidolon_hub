"""Pure functions of the 周边好生活 protocol: signatures, Basic auth, password.

Everything here is checked against the worked examples in the protocol document
(v1.0.5, pages 5, 8, 9 and 11), so a disagreement with the platform shows up
in a unit test rather than in a 401 at a demo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import quote

# Error codes in the fixed response envelope.
CODE_OK = 0
CODE_UNAUTHORIZED = 401
CODE_INVALID_TOKEN = 30106

# ``status`` of a semantic control call.
CONTROL_OK = 0
CONTROL_NO_REPLY = 1001
CONTROL_NO_DEVICES_BOUND = 1002
CONTROL_NO_MATCH = 1003
CONTROL_FAILED = 1004
CONTROL_DEVICE_OFFLINE = 1005
CONTROL_SOME_FAILED = 1006
CONTROL_SOME_OFFLINE = 1007

SCOPE_CONTROL = "cloud_write"
GRANT_HMAC = "hmac_sign"
GRANT_REFRESH = "refresh_token"
ACCOUNT_PHONE = "PHONE_NUMBER"
ACCOUNT_PSEUDO = "PSEUDO_CODE"

PATH_VALID = "/cloud-api/app/valid"
PATH_ACTIVE = "/cloud-api/app/active"
PATH_TOKEN = "/cloud-api/oauth2/token"
PATH_REFRESH = "/cloud-api/oauth2/refresh"
PATH_HOMES = "/cloud-api/home/list"
PATH_CONTROL = "/cloud-api/device/llm/control"
PATH_PRODUCTS = "/cloud-api/product/info"
PATH_DEVICES = "/cloud-api/device/list"


def hmac_sign(secret_b64: str, *fields: str) -> str:
    """HMAC-SHA256 over the fields joined in the given order, keyed by the
    base64-decoded secret, Base64-encoded. Callers pass fields in dictionary
    order of their names, as the document prescribes."""
    key = base64.b64decode(secret_b64)
    digest = hmac.new(key, "".join(fields).encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def sign_valid(app_secret: str, *, app_id: str, nonce: str, timestamp: int, union_id: str) -> str:
    # appId, nonce, timestamp, unionId
    return hmac_sign(app_secret, app_id, nonce, str(timestamp), union_id)


def sign_active(
    app_secret: str, *, app_id: str, timestamp: int, union_id: str, user_id: str
) -> str:
    # appId, timestamp, unionId, userId
    return hmac_sign(app_secret, app_id, str(timestamp), union_id, user_id)


def sign_password(user_secret: str, *, app_id: str, timestamp: int, user_id: str) -> str:
    # appId, secureMode, timestamp, userId
    return hmac_sign(user_secret, app_id, GRANT_HMAC, str(timestamp), user_id)


def password(user_secret: str, *, app_id: str, timestamp: int, user_id: str) -> str:
    sign = sign_password(user_secret, app_id=app_id, timestamp=timestamp, user_id=user_id)
    return (
        f"appId={app_id}&secureMode={GRANT_HMAC}&timestamp={timestamp}"
        f"&userId={user_id}&sign={quote(sign, safe='')}"
    )


def basic_authorization(app_id: str, app_secret: str) -> str:
    """``Basic base64(appId:md5(appSecret))`` with the md5 as lowercase hex."""
    digest = hashlib.md5(app_secret.encode()).hexdigest()
    return "Basic " + base64.b64encode(f"{app_id}:{digest}".encode()).decode()


def username(app_id: str, user_id: str) -> str:
    return f"{app_id}.{user_id}"


def derive_user_id(host_identity: str) -> str:
    """The platform wants one userId per third-party device, unique under the
    appId, 6–32 alphanumerics. Derive it from the Host's identity so the same
    Host always presents the same user and two Hosts never collide."""
    digest = hashlib.sha256(host_identity.encode()).hexdigest()
    return ("eid" + digest)[:32]
