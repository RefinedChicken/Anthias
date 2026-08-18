"""Player-side models for the Fleet Server integration.

Everything here describes *this device's* view of its own fleet
relationship — identity, pairing state/policy, sync progress, and
commands received from a Fleet Server. None of it is the Fleet
Server's own data model (that lives in the separate, not-yet-built
``anthias_fleet_server`` service, with its own database); this app
only ever holds a thin, Player-local reflection of it.

Absence of an active ``FleetPairing`` row means standalone: nothing
here changes how the device behaves until pairing (a later phase)
actually sets it.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models

from anthias_server.app.models import AUTHORITY_CHOICES, AUTHORITY_LOCAL


class PlayerIdentity(models.Model):
    """This device's stable identity, independent of pairing state.

    A singleton row (see :meth:`get_or_create`) — ``device_id`` is
    generated once, at first access, and never regenerated: pairing,
    unpairing, and re-pairing all see the same UUID for the lifetime
    of the device.
    """

    device_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'fleet_link_player_identity'

    def __str__(self) -> str:
        return str(self.device_id)

    @classmethod
    def get_or_create(cls) -> PlayerIdentity:
        identity, _ = cls.objects.get_or_create(pk=1)
        return identity


class FleetPairing(models.Model):
    """This device's relationship to a Fleet Server, if any.

    Rows are never deleted on unpair — a revoked row is kept for
    pairing history/audit (see the plan's unpairing section), so
    "no active pairing" is expressed as "no row with a non-revoked
    status", not "no row at all". Use :meth:`current` rather than
    querying the table directly.

    A completed pairing (``status='active'``) deliberately leaves all
    four authority fields at their ``local`` default — negotiating
    per-domain authority at approval time is explicit scope left to
    the desired-state sync engine (a later phase), since nothing
    reconciles Asset rows yet and there is no policy to enforce. Read
    "paired with everything still local" as intentional here, not a
    bug; the sync engine phase is what starts flipping these.
    """

    PENDING = 'pending'
    ACTIVE = 'active'
    REVOKED = 'revoked'
    STATUS_CHOICES: ClassVar = [
        (PENDING, 'Pending'),
        (ACTIVE, 'Active'),
        (REVOKED, 'Revoked'),
    ]

    fleet_base_url = models.URLField()
    fleet_org_id = models.TextField(blank=True, null=True)
    # The pairing code pair (plan §8, step 2) — generated here, not by
    # Fleet: ``pairing_user_code`` is the short value shown on-screen
    # for a human to read off and type into the Fleet dashboard;
    # ``pairing_device_code`` is the long secret this device alone
    # knows and echoes back on every poll (fleet_link.tasks.
    # poll_fleet_pairing), proving continuity of the same device
    # across retries. Both are cleared (set back to blank) once the
    # pairing completes or is abandoned — a spent pairing secret has
    # no further use and shouldn't linger in the row. Never confuse
    # ``pairing_device_code`` with ``device_credential`` below: the
    # former only ever proves "same device polling," the latter is
    # the real bearer credential used for every call once paired.
    pairing_user_code = models.CharField(max_length=16, blank=True, null=True)
    pairing_device_code = models.TextField(blank=True, null=True)
    pairing_code_expires_at = models.DateTimeField(blank=True, null=True)
    # Opaque reference to the device credential issued at pairing time.
    # Stored in the clear, same posture as the rest of Anthias's
    # device-level config (``anthias.conf``) — there is no OS-keyring
    # integration in this codebase. Fleet-side revocation, not
    # device-side secrecy, is the actual mitigation for a stolen
    # device; see the plan's security-considerations section.
    device_credential = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=8, choices=STATUS_CHOICES, default=PENDING
    )

    # Independent per-domain authority — never collapse these into one
    # "managed" boolean. Each is 'local' or 'fleet'; a future policy
    # like "fleet-managed content but locally-controlled schedule" is
    # representable today without a schema change.
    content_authority = models.CharField(
        max_length=8, choices=AUTHORITY_CHOICES, default=AUTHORITY_LOCAL
    )
    playlist_authority = models.CharField(
        max_length=8, choices=AUTHORITY_CHOICES, default=AUTHORITY_LOCAL
    )
    schedule_authority = models.CharField(
        max_length=8, choices=AUTHORITY_CHOICES, default=AUTHORITY_LOCAL
    )
    config_authority = models.CharField(
        max_length=8, choices=AUTHORITY_CHOICES, default=AUTHORITY_LOCAL
    )

    paired_at = models.DateTimeField(blank=True, null=True)
    unpaired_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fleet_link_pairing'

    def __str__(self) -> str:
        return f'{self.fleet_base_url} ({self.status})'

    @classmethod
    def current(cls) -> FleetPairing | None:
        """The pairing row governing this device right now, if any.

        ``None`` (or every row being ``revoked``) means standalone.
        By construction at most one row is ever non-revoked at a time,
        so "most recent non-revoked row" is unambiguous.
        """
        return (
            cls.objects.exclude(status=cls.REVOKED)
            .order_by('-created_at')
            .first()
        )


class SyncState(models.Model):
    """Per-domain desired-vs-applied sync progress, one row per domain.

    Written by the (not-yet-built) sync engine; exists now so the
    schema is in place ahead of that work. Never touched by the
    existing reachability prober (``Asset.is_reachable`` /
    ``last_reachability_check``) — that's Player-owned runtime state,
    not a desired-state domain, and must never be reported as a sync
    diff.
    """

    MEDIA = 'media'
    CONTENT = 'content'
    CONFIG = 'config'
    DOMAIN_CHOICES: ClassVar = [
        (MEDIA, 'Media'),
        (CONTENT, 'Content'),
        (CONFIG, 'Config'),
    ]

    domain = models.CharField(
        max_length=16, choices=DOMAIN_CHOICES, unique=True
    )
    desired_version = models.IntegerField(blank=True, null=True)
    applied_version = models.IntegerField(blank=True, null=True)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    last_success_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'fleet_link_sync_state'

    def __str__(self) -> str:
        return (
            f'{self.domain}: {self.applied_version} -> {self.desired_version}'
        )


class Command(models.Model):
    """A command received from the Fleet Server, tracked to completion.

    Deliberately separate from ``SyncState``: commands are one-shot
    imperative actions with their own lifecycle, not a desired-state
    domain. ``command_id`` is the Fleet-issued idempotency key — a
    command redelivered after a dropped connection (poll retry, WS
    reconnect) must execute at most once, so lookups/creates always go
    through it rather than the local auto PK.
    """

    REBOOT = 'reboot'
    SHUTDOWN = 'shutdown'
    RESTART_PLAYER = 'restart_player'
    REFRESH_CONTENT = 'refresh_content'
    TAKE_SCREENSHOT = 'take_screenshot'
    UPDATE_SOFTWARE = 'update_software'
    TYPE_CHOICES: ClassVar = [
        (REBOOT, 'Reboot'),
        (SHUTDOWN, 'Shutdown'),
        (RESTART_PLAYER, 'Restart player'),
        (REFRESH_CONTENT, 'Refresh content'),
        (TAKE_SCREENSHOT, 'Take screenshot'),
        (UPDATE_SOFTWARE, 'Update software'),
    ]

    PENDING = 'pending'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    STATUS_CHOICES: ClassVar = [
        (PENDING, 'Pending'),
        (RUNNING, 'Running'),
        (SUCCEEDED, 'Succeeded'),
        (FAILED, 'Failed'),
    ]

    command_id = models.TextField(unique=True)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=PENDING
    )
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'fleet_link_command'
        ordering: ClassVar = ['-created_at']

    def __str__(self) -> str:
        return f'{self.type} ({self.status})'
