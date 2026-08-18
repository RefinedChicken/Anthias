"""Fixtures for the Fleet Server's own pytest suite.

Run as a fully separate invocation (own ``DJANGO_SETTINGS_MODULE``,
own SQLite test DB — see CLAUDE.md's "Fleet Server dev commands"), so
nothing here is shared with the repo root ``conftest.py`` (Player-side
only, and guarded to no-op when the Player app isn't importable —
which it isn't under this settings module).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _clear_throttle_cache() -> Iterator[None]:
    """Reset Django's default cache before every test.

    DRF's ``ScopedRateThrottle`` (the ``pairing`` scope,
    ``api.pairing_views``) and the per-device-code poll gate
    (``api.pairing_views._device_code_poll_allowed``) both count
    against Django's default cache, which defaults to an in-process
    ``LocMemCache`` that otherwise persists for the life of the pytest
    worker — see the identical fixture in the repo root
    ``conftest.py`` for the full rationale.
    """
    from django.core.cache import cache

    cache.clear()
    yield
