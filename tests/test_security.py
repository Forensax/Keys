from __future__ import annotations

from app.security import (
    decrypt_api_key,
    derive_fernet_key,
    encrypt_api_key,
    hash_password,
    key_hint,
    random_salt,
    verify_password,
)


def test_password_hash_verification() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password(password_hash, "correct horse battery staple")
    assert not verify_password(password_hash, "wrong password")


def test_api_key_encryption_round_trip_hides_plaintext() -> None:
    password = "correct horse battery staple"
    salt = random_salt()
    api_key = "sk-test-secret-value"

    encrypted = encrypt_api_key(api_key, password, salt)

    assert api_key not in encrypted
    assert decrypt_api_key(encrypted, password, salt) == api_key
    assert derive_fernet_key(password, salt) != derive_fernet_key("wrong password", salt)


def test_key_hint_masks_secret() -> None:
    assert key_hint("sk-test-secret-value") == "sk-...alue"
    assert key_hint("abcd") == "...abcd"
