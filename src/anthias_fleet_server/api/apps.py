from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'anthias_fleet_server.api'
    label = 'fleet_api'
