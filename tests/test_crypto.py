import base64

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from outpost import crypto

SALT_HEX = "00" * 16


def test_derive_key_is_deterministic():
    key1 = crypto.derive_key("hunter2", SALT_HEX, 1000)
    key2 = crypto.derive_key("hunter2", SALT_HEX, 1000)
    assert key1 == key2
    assert len(key1) == crypto.KEY_LENGTH


@pytest.mark.parametrize(
    ("password", "salt_hex", "iterations"),
    [
        ("different-password", SALT_HEX, 1000),
        ("hunter2", "ff" * 16, 1000),
        ("hunter2", SALT_HEX, 2000),
    ],
)
def test_derive_key_varies_with_inputs(password, salt_hex, iterations):
    baseline = crypto.derive_key("hunter2", SALT_HEX, 1000)
    varied = crypto.derive_key(password, salt_hex, iterations)
    assert varied != baseline


def test_encrypt_output_shape():
    key = crypto.derive_key("hunter2", SALT_HEX, 1000)
    encrypted = crypto.encrypt("secret pane content", key)

    iv_b64, ciphertext_b64 = encrypted.split(":")
    iv = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(ciphertext_b64)

    assert len(iv) == crypto.IV_LENGTH
    assert len(ciphertext) > 0


def test_encrypt_uses_random_iv():
    key = crypto.derive_key("hunter2", SALT_HEX, 1000)
    first = crypto.encrypt("same content", key)
    second = crypto.encrypt("same content", key)
    assert first != second


def test_encrypt_round_trips_with_aesgcm():
    key = crypto.derive_key("hunter2", SALT_HEX, 1000)
    plaintext = "\U0001f9d1 hello, outpost!"
    encrypted = crypto.encrypt(plaintext, key)

    iv_b64, ciphertext_b64 = encrypted.split(":")
    iv = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(ciphertext_b64)

    decrypted = AESGCM(key).decrypt(iv, ciphertext, None)
    assert decrypted.decode() == plaintext
