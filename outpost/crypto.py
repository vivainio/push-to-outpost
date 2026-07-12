import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LENGTH = 32  # AES-256
IV_LENGTH = 12  # standard GCM nonce size


def derive_key(password: str, salt_hex: str, iterations: int) -> bytes:
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=KEY_LENGTH)


def encrypt(content: str, key: bytes) -> str:
    iv = os.urandom(IV_LENGTH)
    ciphertext = AESGCM(key).encrypt(iv, content.encode(), None)
    return f"{base64.b64encode(iv).decode()}:{base64.b64encode(ciphertext).decode()}"
