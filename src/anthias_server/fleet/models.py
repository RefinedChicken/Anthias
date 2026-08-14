import json
from typing import ClassVar, cast

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
    # Telemetry below is populated from the same v2/info payload the
    # heartbeat sweep already fetches every cycle (see fleet/tasks.py)
    # — latest-snapshot only, no history table. `None`/'' consistently
    # means "never successfully polled", distinct from a real 0/off/
    # False value.
    loadavg_15min = models.FloatField(blank=True, null=True)
    # Already human-formatted (django's filesizeformat, e.g. "1.2 GB")
    # by the player's own /v2/info — display verbatim, never parse
    # back to bytes for thresholding.
    free_space = models.TextField(blank=True)
    display_power = models.TextField(blank=True)
    up_to_date = models.BooleanField(blank=True, null=True, default=None)
    uptime_days = models.IntegerField(blank=True, null=True)
    uptime_hours = models.FloatField(blank=True, null=True)
    # Verbatim get_info()['memory'] dict: total/used/free/shared/buff/
    # available/low_ram (all MB except low_ram, which is a bool).
    memory = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Load-average badge thresholds. Not core-count-normalized —
    # v2/info's loadavg is a bare 15-min average and doesn't expose
    # core count, and Anthias spans everything from Pi Zero to x86 —
    # so these are coarse, absolute cutoffs rather than a per-device
    # "% of capacity" figure.
    LOAD_AVG_WARN = 2.0
    LOAD_AVG_CRIT = 4.0

    class Meta:
        db_table = 'fleet_players'
        ordering: ClassVar[list[str]] = ['name']

    def __str__(self) -> str:
        return self.name

    def set_api_token(self, raw_token: str) -> None:
        self.api_token_encrypted = encrypt_api_token(raw_token)

    def get_api_token(self) -> str:
        return decrypt_api_token(self.api_token_encrypted)

    @property
    def health_flags(self) -> list[dict[str, str]]:
        """Badges for the console UI, computed at read time from the
        latest telemetry snapshot so they never drift from stale
        stored thresholds. Each entry is
        {'level': 'info'|'warning'|'critical', 'label': str}.
        """
        flags: list[dict[str, str]] = []

        if self.up_to_date is False:
            flags.append({'level': 'info', 'label': 'Update available'})

        if self.loadavg_15min is not None:
            if self.loadavg_15min >= self.LOAD_AVG_CRIT:
                flags.append(
                    {'level': 'critical', 'label': 'High load average'}
                )
            elif self.loadavg_15min >= self.LOAD_AVG_WARN:
                flags.append(
                    {'level': 'warning', 'label': 'Elevated load average'}
                )

        if self.memory.get('low_ram'):
            flags.append({'level': 'info', 'label': 'Low-RAM device'})

        if self.display_power == 'off':
            flags.append({'level': 'info', 'label': 'Display off'})

        return flags


class AssetPushJob(models.Model):
    """One "push this asset to every player in a group" request. No
    aggregate status field — "still running" is computed at read time
    from ``targets.filter(status=PENDING).exists()`` rather than
    cached, so a last-target-out race can't leave a stale flag.
    """

    group = models.ForeignKey(
        PlayerGroup, on_delete=models.CASCADE, related_name='push_jobs'
    )
    source_player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='+'
    )
    source_asset_id = models.TextField()
    # Snapshot of the source asset's metadata at dispatch time —
    # independent of it being renamed/deleted/mimetype-changed
    # afterwards, and lets every per-target task skip re-contacting
    # source_player entirely (only the staged file, if any, and each
    # target player are touched per-target).
    asset_name = models.TextField(blank=True)
    mimetype = models.CharField(max_length=16, blank=True)
    duration = models.PositiveIntegerField(default=10)
    # The source asset's own uri — used directly for URL-type assets
    # (mimetype='webpage', or an image/video asset backed by a remote
    # URL rather than an uploaded file). Ignored for file-type assets,
    # which upload the staged bytes and get a fresh per-target uri.
    source_uri = models.TextField(blank=True)
    # '' for URL-type assets (nothing to stage); a path under
    # settings.FLEET_PUSH_STAGING_DIR for file-type assets.
    staged_content_path = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'fleet_asset_push_jobs'
        ordering: ClassVar[list[str]] = ['-created_at']


class AssetPushJobTarget(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_SKIPPED = 'skipped'
    STATUS_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_SKIPPED, 'Skipped'),
    ]

    job = models.ForeignKey(
        AssetPushJob, on_delete=models.CASCADE, related_name='targets'
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='+'
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'fleet_asset_push_job_targets'
        ordering: ClassVar[list[str]] = ['player__name']


class PlaylistTemplate(models.Model):
    """A named, ordered set of URI-based items (webpage, or a remote
    image/video URL — no locally-uploaded file content in this first
    cut, see PlaylistTemplateItem) associated with one PlayerGroup.
    "Apply to group" materializes the items onto every member's own
    playlist via PlaylistTemplatePlacement below; editing a template
    does NOT auto-reapply to already-member players — that stays an
    explicit action so a mid-edit save can't push a half-finished
    template.
    """

    name = models.TextField(unique=True)
    description = models.TextField(blank=True)
    group = models.ForeignKey(
        PlayerGroup,
        on_delete=models.CASCADE,
        related_name='playlist_templates',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fleet_playlist_templates'
        ordering: ClassVar[list[str]] = ['name']

    def __str__(self) -> str:
        return self.name


class PlaylistTemplateItem(models.Model):
    template = models.ForeignKey(
        PlaylistTemplate, on_delete=models.CASCADE, related_name='items'
    )
    name = models.TextField()
    uri = models.URLField()
    mimetype = models.CharField(
        max_length=16,
        choices=[
            ('image', 'Image'),
            ('video', 'Video'),
            ('webpage', 'Webpage'),
        ],
    )
    duration = models.PositiveIntegerField(default=10)
    order = models.PositiveIntegerField(default=0)
    is_enabled = models.BooleanField(default=True)
    # JSON-encoded list[int] (1=Monday..7=Sunday, same convention as
    # CreateAssetSerializerV2.play_days / _normalise_play_days) — ''
    # means "every day".
    play_days = models.TextField(blank=True)
    play_time_from = models.TimeField(null=True, blank=True)
    play_time_to = models.TimeField(null=True, blank=True)

    class Meta:
        db_table = 'fleet_playlist_template_items'
        ordering: ClassVar[list[str]] = ['order']

    def __str__(self) -> str:
        return self.name

    def get_play_days(self) -> list[int]:
        """Parse play_days into the stored list of ints (1-7), or every
        day if blank — mirrors Asset.get_play_days()'s "'' means every
        day" contract, minus that method's malformed-JSON fallback
        since this field is only ever written by _parse_play_days()."""
        if not self.play_days:
            return list(range(1, 8))
        return cast('list[int]', json.loads(self.play_days))


class PlaylistTemplatePlacement(models.Model):
    """Fleet-side-only tracking of which remote asset_id on which
    player materializes which template item — substitutes for
    metadata-tagging, which isn't viable: CreateAssetSerializerV2/
    UpdateAssetSerializerV2 (api/serializers/v2.py) don't accept
    arbitrary ``metadata``, only refresh_interval_s/custom_headers
    round-trip. Materialization only ever creates/updates/deletes
    assets tracked here for a given (item, player) pair — it never
    touches anything else on the player, which is what lets
    template-managed and locally-managed assets coexist.

    ``item`` is SET_NULL (not CASCADE) deliberately: deleting a
    template item must not silently drop its placements, or the next
    "Apply to group" would have no record left to prune the
    now-orphaned remote copy from each player — it would just leak
    forever. A placement with ``item=None`` is exactly the signal the
    materialization task's prune step looks for. ``template`` is kept
    as its own FK (not derived via ``item.template``) so it still
    resolves once ``item`` is null, and so deleting the whole template
    still cascades every placement away regardless of per-item state.
    """

    template = models.ForeignKey(
        PlaylistTemplate,
        on_delete=models.CASCADE,
        related_name='placements',
    )
    item = models.ForeignKey(
        PlaylistTemplateItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='placements',
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='+'
    )
    remote_asset_id = models.TextField()
    last_applied_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fleet_playlist_template_placements'
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=['item', 'player'],
                name='unique_placement_per_item_player',
            )
        ]


class TemplateApplicationJob(models.Model):
    """Deliberately not sharing a base class with AssetPushJob despite
    the structural similarity — matches this codebase's existing
    low-abstraction style (players_bulk_action/player_assets_bulk_action
    are two independent loops, not one generic helper)."""

    template = models.ForeignKey(
        PlaylistTemplate,
        on_delete=models.CASCADE,
        related_name='application_jobs',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'fleet_template_application_jobs'
        ordering: ClassVar[list[str]] = ['-created_at']


class TemplateApplicationJobTarget(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES: ClassVar[list[tuple[str, str]]] = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FAILED, 'Failed'),
    ]

    job = models.ForeignKey(
        TemplateApplicationJob,
        on_delete=models.CASCADE,
        related_name='targets',
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='+'
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'fleet_template_application_job_targets'
        ordering: ClassVar[list[str]] = ['player__name']
