from sqm_service.security import hash_password, hash_token, verify_password


def test_password_hash_round_trip():
    encoded = hash_password("a sufficiently long test password", iterations=10_000)
    assert verify_password("a sufficiently long test password", encoded)
    assert not verify_password("the wrong password", encoded)
    assert "sufficiently long" not in encoded


def test_token_hash_is_stable_and_not_plaintext():
    assert hash_token("session-token") == hash_token("session-token")
    assert hash_token("session-token") != "session-token"

