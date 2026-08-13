from django.contrib import admin

from anthias_server.api.models import AnthiasAPIToken


@admin.register(AnthiasAPIToken)
class AnthiasAPITokenAdmin(admin.ModelAdmin[AnthiasAPIToken]):
    # token_hash is deliberately excluded — it's a credential digest,
    # not operator-facing data, even in the admin.
    list_display = (
        'name',
        'prefix',
        'user',
        'created_at',
        'last_used_at',
        'expires_at',
    )
    readonly_fields = ('token_hash', 'prefix', 'created_at', 'last_used_at')
