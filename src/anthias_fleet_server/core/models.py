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


class PlayerCredential(models.Model):
    """A bearer credential scoped to exactly one Player — never a human
    ``User`` (see the plan's authentication-design section on why
    human and device credentials never share a table). Same
    hash-not-plaintext pattern as the Player-side ``AnthiasAPIToken``:
    only ``token_hash`` is ever persisted, the raw value is returned
    to the Player exactly once (at pairing-poll delivery time, see
    ``api.pairing_views``) and can't be recovered afterwards.

    Rotation is "issue a new row, revoke the old one" rather than an
    in-place mutation — ``revoked_at`` on the superseded row is the
    audit trail. Not constrained to one non-revoked row per Player at
    the database level: the pairing hand-off deliberately reissues a
    fresh credential on every poll until the Player acknowledges
    receipt (see ``PairingRequest``), which transiently produces more
    than one live row for the same Player by design.
    """

    player = models.ForeignKey(
        'Player', on_delete=models.CASCADE, related_name='credentials'
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=12)
    issued_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    last_used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'player_credentials'
        ordering: ClassVar = ['-issued_at']

    def __str__(self) -> str:
        state = 'revoked' if self.revoked_at else 'active'
        return f'{self.prefix}… ({self.player}, {state})'


class PairingRequest(models.Model):
    """One Player's in-progress (or completed) pairing handshake.

    The pairing code pair is Player-generated, not Fleet-issued (see
    the plan's pairing-protocol section, step 2) — ``user_code`` is
    the short value a human reads off the Player's screen and types
    into the Fleet dashboard to approve; ``device_code_hash`` is the
    SHA-256 of the long secret the Player alone knows and echoes back
    on every poll, proving continuity of the same polling device
    across retries (same hash-not-plaintext posture as
    ``PlayerCredential``/``AnthiasAPIToken`` — the raw device code is
    never stored). This row is created on the *first* poll that
    presents a given device code (there is no separate "register"
    call — see ``api.pairing_views.PairingPollView``), which is why
    ``organization`` starts out null: nobody has authenticated yet, so
    there is nothing to scope it to. It's filled in at approval time
    from the *approving admin's* ``Membership``, the same scoping
    every other Fleet view already uses — never inferred from
    ``Organization.objects.first()``, which is "alphabetically first
    org", not "the" org, and is wrong the moment a second org exists.

    ``status`` has three states, not two: ``pending`` (waiting for an
    admin), ``approved`` (admin approved; a credential is being
    handed out on each poll until the Player proves it received one),
    and ``completed`` (the Player called ``/api/pairing/ack`` with
    that credential — the handshake is over, further polls are inert).
    The reissue-until-ack behaviour in the ``approved`` state is what
    makes an interrupted hand-off (Player approved, but the poll
    response carrying the credential never arrived) safe to resume:
    the previous credential is revoked and a fresh one issued each
    time, so no lost-credential state is ever unrecoverable — see the
    plan's "interrupted pairing" requirement.
    """

    PENDING = 'pending'
    APPROVED = 'approved'
    COMPLETED = 'completed'
    STATUS_CHOICES: ClassVar = [
        (PENDING, 'Pending'),
        (APPROVED, 'Approved'),
        (COMPLETED, 'Completed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='pairing_requests',
        blank=True,
        null=True,
    )
    user_code = models.CharField(max_length=16, db_index=True)
    device_code_hash = models.CharField(max_length=64, unique=True)
    device_id = models.UUIDField(blank=True, null=True)
    label = models.TextField(blank=True)
    status = models.CharField(
        max_length=9, choices=STATUS_CHOICES, default=PENDING
    )
    player = models.ForeignKey(
        'Player',
        on_delete=models.SET_NULL,
        related_name='pairing_requests',
        blank=True,
        null=True,
    )
    # The credential most recently handed out for this request but
    # not yet acknowledged — see the class docstring's "reissue until
    # ack" note. Cleared (not just left dangling) once acknowledged;
    # SET_NULL rather than CASCADE so revoking/deleting a credential
    # row never takes this request row down with it.
    pending_credential = models.ForeignKey(
        PlayerCredential,
        on_delete=models.SET_NULL,
        related_name='+',
        blank=True,
        null=True,
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='pairing_requests_approved',
        blank=True,
        null=True,
    )
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'pairing_requests'
        ordering: ClassVar = ['-created_at']

    def __str__(self) -> str:
        return f'{self.user_code} ({self.status})'


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
