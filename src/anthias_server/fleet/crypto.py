"""Fernet encryption for ``Player.api_token_encrypted`` at rest.

The fleet DB holds a live bearer token for every registered player,
making it a much higher-value target than any single player's own
DB — encrypting at rest is cheap now and expensive to retrofit later.
Keyed off this fleet server's own ``django_secret_key`` (never leaves
the device — same trust boundary ``anthias_common.internal_auth``
already relies on for its HMAC), so no separate key-management story
is needed.

Only ever imported by the ``fleet`` app, which is itself only
installed when ``ANTHIAS_SERVICE == 'fleet'`` — unlike ``lib.auth``,
this doesn't need a lazy-import trick to stay out of the viewer's
import graph.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    from anthias_server.settings import settings

    secret = settings['django_secret_key'] or ''
    if not secret:
        # settings.py auto-generates django_secret_key on first Django
        # startup, before any view can run — this only fires if
        # something bypassed that (e.g. a hand-edited conf file).
        raise RuntimeError(
            'django_secret_key is not set; cannot encrypt API tokens.'
        )
    # Fernet requires a 32-byte url-safe base64-encoded key; derive one
    # deterministically from the arbitrary-length secret so no second
    # key needs to be generated or stored anywhere.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_api_token(raw_token: str) -> str:
    return _fernet().encrypt(raw_token.encode()).decode()


def decrypt_api_token(encrypted_token: str) -> str:
    return _fernet().decrypt(encrypted_token.encode()).decode()
