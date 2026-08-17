from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'anthias_fleet_server.core'
    label = 'fleet_core'
