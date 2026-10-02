"""Signatures against the worked examples in 《云云对接协议-周边好生活》v1.0.5."""

from hub.smarthome.providers.zhoubian import protocol as p

APP_ID = "pEOHuobHzgkkqVZQ"
APP_SECRET = "3LA3PK2OKdsi+MZoVuOew1ANkTqYBRjJ"


def test_valid_signature_matches_page_5():
    assert (
        p.sign_valid(
            APP_SECRET,
            app_id=APP_ID,
            nonce="9748fdaae1a7c3019748fdaae1a7c301",
            timestamp=1740972803,
            union_id="537488",
        )
        == "G18NVEsTrMvaaiSw2bQMq0v31l1SMyRANoJLhY3RnOY="
    )


def test_active_signature_matches_page_8():
    assert (
        p.sign_active(
            APP_SECRET,
            app_id=APP_ID,
            timestamp=1734589472,
            union_id="537488",
            user_id="0057030220031000170l".replace("l", "1"),
        )
        == "PaAjVHRvl7oYO+RCm55Pt7CFoC5LN566ADqWQj3xVM8="
    )


def test_basic_authorization_matches_page_9():
    assert (
        p.basic_authorization(APP_ID, APP_SECRET)
        == "Basic cEVPSHVvYkh6Z2trcVZaUTphNWQ5Mjk5YjYyNzFlMjZkZDVhYTQ1ZWI3MGViZGM1MQ=="
    )


def test_password_matches_page_11():
    user_secret = "rim7jndwfC2HA1P4SsApqQ=="
    assert (
        p.sign_password(
            user_secret, app_id=APP_ID, timestamp=1734593549, user_id="00570302200310001701"
        )
        == "50Yjg5yEsY9OFTPgClrOElVeWN5WNRoXK+HBOjbcoug="
    )
    assert p.password(
        user_secret, app_id=APP_ID, timestamp=1734593549, user_id="00570302200310001701"
    ) == (
        "appId=pEOHuobHzgkkqVZQ&secureMode=hmac_sign&timestamp=1734593549"
        "&userId=00570302200310001701&sign=50Yjg5yEsY9OFTPgClrOElVeWN5WNRoXK%2BHBOjbcoug%3D"
    )


def test_user_id_is_stable_and_within_bounds():
    one, two = p.derive_user_id("host-a"), p.derive_user_id("host-b")
    assert one != two and one == p.derive_user_id("host-a")
    assert 6 <= len(one) <= 32 and one.isalnum()
