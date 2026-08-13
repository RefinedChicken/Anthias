"""Page-rendering helpers for the fleet console.

Deliberately NOT ``anthias_server.app.helpers.template()``: that
helper's context is built for a *player* page (default asset
duration/date-format meta tags for the Flatpickr widgets, the Apps
store-index URL, ``is_up_to_date()``'s GitHub-release check, ...) —
none of which the fleet console's own pages read. Reusing it here
would drag in per-device settings that happen to be harmless no-ops in
fleet mode, but that's accidental, not a contract this module wants to
depend on. This is the fleet-console equivalent: it only adds
``navbar_template`` so ``_layout.html`` renders
``fleet/_fleet_navbar.html`` instead of the player app's own navbar.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

FLEET_NAVBAR_TEMPLATE = 'fleet/_fleet_navbar.html'


def template(
    request: HttpRequest,
    template_name: str,
    context: dict[str, Any],
) -> HttpResponse:
    """Render a full fleet-console page through the shared
    ``_layout.html``/``base.html`` chrome, swapped to the fleet navbar.
    """
    context.setdefault('navbar_template', FLEET_NAVBAR_TEMPLATE)
    return render(request, template_name, context)
