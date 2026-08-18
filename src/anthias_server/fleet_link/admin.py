from django.contrib import admin

from anthias_server.fleet_link.models import (
    Command,
    FleetPairing,
    PlayerIdentity,
    SyncState,
)


@admin.register(PlayerIdentity)
class PlayerIdentityAdmin(admin.ModelAdmin[PlayerIdentity]):
    list_display = ('device_id', 'created_at')
    readonly_fields = ('device_id', 'created_at')


@admin.register(FleetPairing)
class FleetPairingAdmin(admin.ModelAdmin[FleetPairing]):
    list_display = (
        'fleet_base_url',
        'status',
        'pairing_user_code',
        'content_authority',
        'playlist_authority',
        'schedule_authority',
        'config_authority',
        'paired_at',
        'unpaired_at',
    )
    # device_credential/pairing_device_code are deliberately excluded
    # from list_display — bearer secrets, not operator-facing summary
    # data, even in the admin.
    readonly_fields = (
        'device_credential',
        'pairing_device_code',
        'created_at',
        'updated_at',
    )


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin[SyncState]):
    list_display = (
        'domain',
        'desired_version',
        'applied_version',
        'last_attempt_at',
        'last_success_at',
    )
    readonly_fields = ('domain',)


@admin.register(Command)
class CommandAdmin(admin.ModelAdmin[Command]):
    list_display = (
        'command_id',
        'type',
        'status',
        'created_at',
        'completed_at',
    )
    readonly_fields = ('command_id', 'created_at')
