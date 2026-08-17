"""Fleet Server domain models.

This is the control plane's own database (PostgreSQL) — entirely
separate from a Player's local SQLite. See CLAUDE.md's "Two-product
direction" section for the split this app is half of.

Phase 3 scope: the data model plus admin-only CRUD (see ``core.admin``
and ``api``). Nothing here resolves a Deployment into concrete desired
state yet, and nothing pairs a Player row to a real device — those are
later phases (pairing, desired-state sync).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models


class Organization(models.Model):
    """Single-row today by design.

    The FK exists on every other model so multi-tenancy is
    representable later without a schema change, but building real
    tenant isolation (org-scoped auth boundaries, a switcher UI, etc.)
    is explicitly out of scope until it's actually asked for — see the
    plan's "Scope discipline" note.
    """

    name = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'organizations'
        ordering: ClassVar = ['name']

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    """A human User's role within an Organization.

    Distinct from "is this User's login valid" (that's plain Django
    auth) — this is what they're allowed to *do*. Roles are ordered
    Owner > Administrator > Operator > Viewer; see
    ``api.permissions.HasMinimumRole`` for how a view enforces a
    minimum role against this.
    """

    OWNER = 'owner'
    ADMINISTRATOR = 'administrator'
    OPERATOR = 'operator'
    VIEWER = 'viewer'
    ROLE_CHOICES: ClassVar = [
        (OWNER, 'Owner'),
        (ADMINISTRATOR, 'Administrator'),
        (OPERATOR, 'Operator'),
        (VIEWER, 'Viewer'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='memberships'
    )
    role = models.CharField(
        max_length=16, choices=ROLE_CHOICES, default=VIEWER
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'memberships'
        ordering: ClassVar = ['created_at']
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=['user', 'organization'], name='unique_membership'
            )
        ]

    def __str__(self) -> str:
        return f'{self.user} @ {self.organization} ({self.role})'


class Group(models.Model):
    """A named set of Players within an Organization (e.g. "Lobby",
    "Chapel") — a Deployment can target a Group instead of a single
    Player."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='groups'
    )
    name = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'groups'
        ordering: ClassVar = ['name']
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=['organization', 'name'], name='unique_group_name'
            )
        ]

    def __str__(self) -> str:
        return self.name


class Player(models.Model):
    """A registered device record.

    ``device_id`` mirrors the UUID a Player generates for itself
    locally (``fleet_link.models.PlayerIdentity.device_id`` in the
    Player codebase) — nullable here because Phase 3 has no pairing
    flow yet. An admin can create a Player row by hand ahead of a
    device ever pairing; the pairing phase fills this in (or matches
    against it) rather than replacing this model.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='players'
    )
    name = models.TextField()
    device_id = models.UUIDField(blank=True, null=True, unique=True)
    groups = models.ManyToManyField(Group, blank=True, related_name='players')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'players'
        ordering: ClassVar = ['name']

    def __str__(self) -> str:
        return self.name


class Media(models.Model):
    """Canonical content library entry.

    Uploaded once, deployed to many Players via Playlist membership —
    identity is ``id`` (uuid4, immutable) plus ``checksum``, never the
    filename, mirroring the Player's own ``Asset.asset_id``/``md5``
    convention. ``file`` uses Django's storage abstraction (local disk
    today; swapping to S3-compatible storage later is a ``STORAGES``
    config change, not a rewrite — see the plan's content-distribution
    section). The per-organization checksum constraint is the "upload
    once" guarantee: re-uploading identical bytes reuses the row
    instead of silently duplicating storage.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='media'
    )
    file = models.FileField(upload_to='media/%Y/%m/')
    filename = models.TextField()
    mimetype = models.TextField(blank=True)
    size = models.BigIntegerField()
    checksum = models.CharField(max_length=64, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'media'
        ordering: ClassVar = ['-created_at']
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=['organization', 'checksum'],
                name='unique_media_checksum_per_org',
            )
        ]

    def __str__(self) -> str:
        return self.filename


class Playlist(models.Model):
    """A named, ordered, versioned list of Media.

    ``version`` is bumped by :meth:`bump_version` whenever the
    playlist's items change — this is the number a Deployment
    snapshots and a Player's ``SyncState.desired_version`` (a later
    phase) compares against. It is not the same thing as the row's PK.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='playlists'
    )
    name = models.TextField()
    version = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'playlists'
        ordering: ClassVar = ['name']

    def __str__(self) -> str:
        return f'{self.name} (v{self.version})'

    def bump_version(self) -> None:
        """Atomically increment ``version`` (F-expression update, so
        concurrent item edits can't race-lose an increment) and load
        the new value back onto this instance."""
        Playlist.objects.filter(pk=self.pk).update(
            version=models.F('version') + 1
        )
        self.refresh_from_db(fields=['version', 'updated_at'])


class PlaylistItem(models.Model):
    """One piece of Media's placement within a Playlist, with its own
    ordering and optional schedule override — mirrors the fields a
    Player's ``Asset`` row needs once this item is materialized down
    to a device (see the plan's playlists/scheduling section)."""

    playlist = models.ForeignKey(
        Playlist, on_delete=models.CASCADE, related_name='items'
    )
    media = models.ForeignKey(
        Media, on_delete=models.PROTECT, related_name='playlist_items'
    )
    order = models.IntegerField(default=0)
    duration = models.BigIntegerField(blank=True, null=True)
    play_days = models.JSONField(default=list, blank=True)
    play_time_from = models.TimeField(blank=True, null=True)
    play_time_to = models.TimeField(blank=True, null=True)
    start_date = models.DateTimeField(blank=True, null=True)
    end_date = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'playlist_items'
        ordering: ClassVar = ['playlist', 'order']

    def __str__(self) -> str:
        return f'{self.playlist.name}[{self.order}] = {self.media.filename}'


class Deployment(models.Model):
    """A specific Playlist version targeted at a Player or a Group —
    never both, never neither (enforced by the constraint below). This
    is what turns "a playlist" into "a playlist actually assigned
    somewhere"; a Player's sync engine (a later phase) resolves the
    Deployments that apply to it into concrete desired state.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='deployments'
    )
    playlist = models.ForeignKey(
        Playlist, on_delete=models.CASCADE, related_name='deployments'
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='deployments',
        blank=True,
        null=True,
    )
    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name='deployments',
        blank=True,
        null=True,
    )
    # Snapshot of playlist.version at creation time — what "this
    # deployment" means never silently drifts if the playlist is
    # edited again later; editing the playlist is what creates the
    # need for a new Deployment, not a mutation of this one.
    version = models.IntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='deployments_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'deployments'
        ordering: ClassVar = ['-created_at']
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=(
                    models.Q(player__isnull=False, group__isnull=True)
                    | models.Q(player__isnull=True, group__isnull=False)
                ),
                name='deployment_targets_player_xor_group',
            )
        ]

    def __str__(self) -> str:
        target = self.player or self.group
        return f'{self.playlist.name} v{self.version} -> {target}'
