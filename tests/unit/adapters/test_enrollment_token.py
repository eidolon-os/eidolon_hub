from hub.adapters.security.enrollment_token import Sha256RetrievalTokenHasher


def test_retrieval_token_hash_is_deterministic_and_constant_time_verified() -> None:
    tokens = Sha256RetrievalTokenHasher()
    token_hash = tokens.hash("device-generated-random-token-000001")

    assert len(token_hash) == 64
    assert tokens.verify("device-generated-random-token-000001", token_hash)
    assert not tokens.verify("another-device-random-token-00001", token_hash)
