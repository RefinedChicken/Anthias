from typing import ClassVar

from django.db import models

from anthias_server.fleet.crypto import decrypt_api_token, encrypt_api_token


class PlayerGroup(models.Model):
    name = models.TextField(unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'fleet_player_groups'
        ordering: ClassVar[list[str]] = ['name']

    def __str__(self) -> str:
        return self.name


class Player(models.Model):
    name = models.TextField()
    # Single-LAN/VPN topology (confirmed design decision) — the fleet
    # server dials directly into this address, no relay/NAT traversal.
    base_url = models.URLField()
    # Fernet-encrypted at rest (see fleet.crypto) — never store the
    # Phase-0 bearer token in plaintext. Use set_api_token()/
    # get_api_token() rather than touching this field directly.
    api_token_encrypted = models.TextField()
    # Mirrors Asset.skip_ssl_verify — players on a self-hosted LAN/VPN
    # commonly run plain HTTP or a self-signed cert.
    skip_ssl_verify = models.BooleanField(default=False)
    group = models.ForeignKey(
        PlayerGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='players',
    )
    # Populated from this player's own v2/info on first successful
    # contact. MAC is the closest thing to a stable device identity
    # that exists anywhere in Anthias today — there's no self-generated
    # device UUID.
    mac_address = models.TextField(blank=True)
    device_model = models.TextField(blank=True)
    anthias_version = models.TextField(blank=True)
    # Field naming deliberately mirrors Asset.is_reachable /
    # Asset.last_reachability_check for codebase-wide consistency.
    is_reachable = models.BooleanField(default=False)
    last_reachability_check = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fleet_players'
        ordering: ClassVar[list[str]] = ['name']

    def __str__(self) -> str:
        return self.name

    def set_api_token(self, raw_token: str) -> None:
        self.api_token_encrypted = encrypt_api_token(raw_token)

    def get_api_token(self) -> str:
        return decrypt_api_token(self.api_token_encrypted)
