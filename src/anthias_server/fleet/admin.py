from django.contrib import admin

from anthias_server.fleet.models import Player, PlayerGroup


@admin.register(PlayerGroup)
class PlayerGroupAdmin(admin.ModelAdmin[PlayerGroup]):
    list_display = ('name', 'description', 'created_at')


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin[Player]):
    # api_token_encrypted is deliberately excluded — it's a credential
    # ciphertext, not operator-facing data, even in the admin.
    list_display = (
        'name',
        'base_url',
        'group',
        'is_reachable',
        'last_reachability_check',
        'mac_address',
        'device_model',
        'anthias_version',
    )
    readonly_fields = (
        'mac_address',
        'device_model',
        'anthias_version',
        'is_reachable',
        'last_reachability_check',
        'last_error',
        'created_at',
        'updated_at',
    )
