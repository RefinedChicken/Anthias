"""URL configuration for ANTHIAS_SERVICE=fleet.

Sibling of ``urls.py`` (the player's urlconf), selected by
``ROOT_URLCONF`` in ``settings.py``. The fleet server has no local
assets, no REST API of its own, and no OpenAPI schema to publish —
those routes in ``urls.py`` are all player-specific — so this stays
deliberately minimal: the fleet app's own views at ``/``, plus
``/admin`` for operator debugging of Player/PlayerGroup rows.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin', admin.site.urls),
    path('', include('anthias_server.fleet.urls')),
]
