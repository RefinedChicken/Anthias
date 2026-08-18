"""Phase 3's "minimal UI": the Django admin site over the core models.

A purpose-built dashboard (players/groups/deployments/audit) is later
work (plan §16/§21 phase 7) — the admin site is a real, working,
server-rendered UI today rather than a placeholder, consistent with
how the rest of Anthias avoids adding new frontend tooling before it's
needed.
"""

from django.contrib import admin

from anthias_fleet_server.core.models import (
    Deployment,
    Group,
    Media,
    Membership,
    Organization,
    PairingRequest,
    Player,
    PlayerCredential,
    Playlist,
    PlaylistItem,
)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin[Organization]):
    list_display = ('name', 'created_at')


class MembershipInline(admin.TabularInline[Membership, Organization]):
    model = Membership
    extra = 0


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin[Membership]):
    list_display = ('user', 'organization', 'role', 'created_at')
    list_filter = ('role', 'organization')


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin[Group]):
    list_display = ('name', 'organization', 'created_at')
    list_filter = ('organization',)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin[Player]):
    list_display = ('name', 'organization', 'device_id', 'created_at')
    list_filter = ('organization', 'groups')
    filter_horizontal = ('groups',)


@admin.register(PlayerCredential)
class PlayerCredentialAdmin(admin.ModelAdmin[PlayerCredential]):
    list_display = ('prefix', 'player', 'issued_at', 'revoked_at')
    list_filter = ('player__organization',)
    readonly_fields = ('token_hash', 'prefix', 'issued_at', 'last_used_at')


@admin.register(PairingRequest)
class PairingRequestAdmin(admin.ModelAdmin[PairingRequest]):
    list_display = (
        'user_code',
        'status',
        'device_id',
        'organization',
        'player',
        'created_at',
        'expires_at',
    )
    list_filter = ('status', 'organization')
    readonly_fields = (
        'id',
        'device_code_hash',
        'created_at',
        'approved_at',
        'completed_at',
    )


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin[Media]):
    list_display = (
        'filename',
        'organization',
        'mimetype',
        'size',
        'checksum',
        'created_at',
    )
    list_filter = ('organization',)
    readonly_fields = ('id', 'checksum', 'size', 'created_at')


class PlaylistItemInline(admin.TabularInline[PlaylistItem, Playlist]):
    model = PlaylistItem
    extra = 0
    ordering = ('order',)


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin[Playlist]):
    list_display = ('name', 'organization', 'version', 'updated_at')
    list_filter = ('organization',)
    readonly_fields = ('version',)
    inlines = (PlaylistItemInline,)


@admin.register(Deployment)
class DeploymentAdmin(admin.ModelAdmin[Deployment]):
    list_display = (
        'playlist',
        'version',
        'player',
        'group',
        'organization',
        'created_by',
        'created_at',
    )
    list_filter = ('organization',)
    readonly_fields = ('created_at',)
