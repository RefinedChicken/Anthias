"""Fleet-side counterpart to the Player's bearer-token auth
(``anthias_server.lib.auth.AnthiasAPITokenAuthentication``) — same
hash-not-plaintext, ``Authorization: Bearer <token>`` shape, but
scoped to a ``PlayerCredential``/``Player`` instead of a human
``User`` (see the plan's authentication-design section on why human
and device credentials never share a table or a validation path).
This is what a Player presents on every outbound call once paired,
starting with ``/api/pairing/ack``; the continuous sync/heartbeat
traffic that will lean on it hardest is a later phase, but the auth
mechanism itself is settled here.
"""

from __future__ import annotations

import hashlib
import secrets

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import (
    BaseAuthentication,
    get_authorization_header,
)
from rest_framework.request import Request

from anthias_fleet_server.core.models import Player, PlayerCredential

# Distinct prefix from the Player's own 'ant_' tokens so a leaked
# value's provenance (fleet-issued device credential vs. Player
# operator token) is unambiguous at a glance.
_CREDENTIAL_PREFIX = 'fpc_'
_CREDENTIAL_DISPLAY_PREFIX_LEN = 12


def generate_player_credential() -> str:
    """A new random device bearer credential, e.g. ``fpc_<43 chars>``.

    Same entropy budget (256 bits via ``secrets.token_urlsafe(32)``)
    as the Player's own ``AnthiasAPIToken`` — see
    ``anthias_server.lib.auth.generate_api_token``.
    """
    return f'{_CREDENTIAL_PREFIX}{secrets.token_urlsafe(32)}'


def hash_player_credential(raw_credential: str) -> str:
    """SHA-256 hex digest, for storage/lookup — mirrors
    ``anthias_server.lib.auth.hash_api_token``. A device credential is
    already high-entropy and machine-generated, not user-chosen, so a
    fast digest is the right primitive here too."""
    return hashlib.sha256(raw_credential.encode()).hexdigest()


def issue_player_credential(player: Player) -> tuple[PlayerCredential, str]:
    """Create and persist a new ``PlayerCredential`` for *player*.

    Returns ``(credential_row, raw_credential)`` — the raw value is
    never stored and this is the only place it's ever available;
    callers must return it to the Player immediately.
    """
    raw_credential = generate_player_credential()
    credential_row = PlayerCredential.objects.create(
        player=player,
        token_hash=hash_player_credential(raw_credential),
        prefix=raw_credential[:_CREDENTIAL_DISPLAY_PREFIX_LEN],
    )
    return credential_row, raw_credential


class AuthenticatedPlayer:
    """Minimal DRF-compatible principal for a credential-authenticated
    Player. Stands in for ``request.user`` where there is no human
    ``User`` row at all, so ``IsAuthenticated`` still works the normal
    DRF way instead of every pairing-protocol view needing a bespoke
    permission check. ``pk`` mirrors the wrapped Player's own pk —
    ``ScopedRateThrottle.get_cache_key`` reads ``request.user.pk``
    unconditionally for any authenticated request, Django ``User`` or
    not.
    """

    is_authenticated = True

    def __init__(self, player: Player) -> None:
        self.player = player
        self.pk = player.pk


class PlayerCredentialAuthentication(BaseAuthentication):
    """``Authorization: Bearer <credential>`` against
    ``PlayerCredential`` — the Fleet-side counterpart to the Player's
    ``AnthiasAPITokenAuthentication``. Never resolves to a human
    ``User``; ``request.user`` is an ``AuthenticatedPlayer`` and
    ``request.auth`` is the ``PlayerCredential`` row itself.
    """

    keyword = b'bearer'

    def authenticate(
        self, request: Request
    ) -> tuple[AuthenticatedPlayer, PlayerCredential] | None:
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword:
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed(
                'Invalid Authorization header. Expected "Bearer <token>".'
            )
        try:
            raw_credential = header[1].decode()
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed(
                'Invalid credential header.'
            ) from exc

        try:
            credential = PlayerCredential.objects.select_related('player').get(
                token_hash=hash_player_credential(raw_credential)
            )
        except PlayerCredential.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(
                'Invalid credential.'
            ) from exc
        if credential.revoked_at is not None:
            raise exceptions.AuthenticationFailed('Credential revoked.')

        # Best-effort last-used bookkeeping — an UPDATE on the single
        # matched row, mirroring AnthiasAPITokenAuthentication.
        PlayerCredential.objects.filter(pk=credential.pk).update(
            last_used_at=timezone.now()
        )
        return (AuthenticatedPlayer(credential.player), credential)

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer'
