from typing import ClassVar

from django.conf import settings
from django.db import models


class AnthiasAPIToken(models.Model):
    """A bearer credential for server-to-server API access.

    Distinct from the operator's own login (session/Basic auth):
    a token is scoped to a single ``User`` (inheriting that user's
    permissions) but is meant for an unattended caller — e.g. the
    fleet-management server calling into this player's REST API —
    rather than a human sitting at the settings page. Only the salted
    hash is ever persisted; the raw value is shown to the operator
    exactly once, at creation time.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='api_tokens',
    )
    name = models.TextField()
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    # First few characters of the raw token, kept in clear so the
    # settings page can show e.g. "ant_a1b2c3…" to help an operator
    # recognise *which* token a row is without ever re-displaying the
    # secret itself (GitHub/Stripe personal-access-token convention).
    prefix = models.CharField(max_length=12)
    # Distinguishes "the token a fleet-management server pairs with"
    # from every other integration token. This is what lets a player
    # know it's fleet-managed at all (see lib.auth.is_fleet_managed) —
    # without this, every AnthiasAPIToken looks identical regardless
    # of who holds it. At most one token should carry the
    # fleet_management purpose at a time (issuing a new one replaces
    # any existing one — see the pairing view), matching the one
    # player-to-one-fleet-server pairing model.
    PURPOSE_GENERAL = 'general'
    PURPOSE_FLEET_MANAGEMENT = 'fleet_management'
    PURPOSE_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (PURPOSE_GENERAL, 'General'),
        (PURPOSE_FLEET_MANAGEMENT, 'Fleet Management'),
    ]
    purpose = models.CharField(
        max_length=32, choices=PURPOSE_CHOICES, default=PURPOSE_GENERAL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'api_tokens'
        ordering: ClassVar[list[str]] = ['-created_at']

    def __str__(self) -> str:
        return f'{self.name} ({self.prefix}…)'
