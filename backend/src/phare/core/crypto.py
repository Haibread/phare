"""Symmetric encryption for secrets at rest (per-profile source tokens).

The Fernet key is derived deterministically from ``SECRET_KEY`` so the same configured secret
always decrypts what it encrypted, without storing a separate key. Encryption is only available
when a secret is configured; callers must check :func:`encryption_available` first.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from phare.core.config import Settings


class EncryptionUnavailableError(RuntimeError):
    """Raised when token encryption is requested but no SECRET_KEY is set."""


def encryption_available(settings: Settings) -> bool:
    return settings.signing_secret is not None


def _fernet(settings: Settings) -> Fernet:
    secret = settings.signing_secret
    if secret is None:
        raise EncryptionUnavailableError("SECRET_KEY must be set to store tokens")
    # Derive a stable 32-byte urlsafe key from the configured secret.
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(settings: Settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt(settings: Settings, token: str) -> str | None:
    """Decrypt; returns ``None`` if the ciphertext doesn't match the current secret."""
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken:
        return None
