"""Celery tasks for the fleet server's heartbeat polling.

Deliberately its own module rather than living in the shared
``celery_tasks.py`` (see that file's docstring/comments on Django
import-order): ``anthias_server.fleet`` is only in ``INSTALLED_APPS``
when running as the fleet service (or under the test runner), so an
unconditional top-level import of ``anthias_server.fleet.models`` from
``celery_tasks.py`` — which is imported by *every* celery worker,
player and fleet alike — would crash the player's worker at import
time with a Django ``RuntimeError`` about the model's app not being
installed. Keeping these two tasks in their own fleet-only module lets
``celery_tasks.py`` import them behind an ``ANTHIAS_SERVICE == 'fleet'``
guard instead.
"""

from __future__ import annotations

import logging

from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from anthias_server.celery_tasks import celery
from anthias_server.fleet.player_client import (
    PlayerAPIClient,
    PlayerAPIError,
    PlayerUnreachableError,
)

logger = logging.getLogger(__name__)

# Time budget for a single player poll. get_info() is bounded by
# PlayerAPIClient's own (3, 5)s connect/read timeout, so a healthy
# poll finishes in well under a second; the soft/hard limits here are
# a backstop against a call stuck below the requests layer (a wedged
# DNS resolver, same failure mode PERIODIC_POKE_*_TIME_LIMIT_S guards
# against in celery_tasks.py) rather than the expected-case budget.
HEARTBEAT_POLL_SOFT_TIME_LIMIT_S = 15
HEARTBEAT_POLL_TIME_LIMIT_S = 30

# Time budget for fanning the sweep out. Each iteration only enqueues
# a task (a Redis publish), so even a fleet of thousands of players
# should fan out in well under this; the limit exists purely so a
# broker hiccup mid-sweep can't wedge the worker indefinitely.
HEARTBEAT_SWEEP_SOFT_TIME_LIMIT_S = 60
HEARTBEAT_SWEEP_TIME_LIMIT_S = 90


@celery.task(
    soft_time_limit=HEARTBEAT_POLL_SOFT_TIME_LIMIT_S,
    time_limit=HEARTBEAT_POLL_TIME_LIMIT_S,
)
def poll_player_heartbeat(player_id: int) -> None:
    """Poll one player's ``/api/v2/info`` and record reachability.

    No-ops if the row is gone (deleted between scheduling and
    execution) rather than raising — the fan-out in ``heartbeat_sweep``
    reads the player table once and dispatches by id, so a delete that
    lands in that window is expected, not an error.
    """
    from anthias_server.fleet.models import Player

    try:
        player = Player.objects.get(id=player_id)
    except Player.DoesNotExist:
        return

    try:
        client = PlayerAPIClient(player)
        info = client.get_info()
    except SoftTimeLimitExceeded:
        # Mirrors the get_display_power / send_telemetry_task pattern
        # in celery_tasks.py: skip this tick cleanly rather than let
        # the hard limit SIGKILL the worker. The next sweep retries.
        logger.warning(
            'poll_player_heartbeat: poll of player %s exceeded %ss; '
            'skipping this tick',
            player_id,
            HEARTBEAT_POLL_SOFT_TIME_LIMIT_S,
        )
        return
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        Player.objects.filter(id=player_id).update(
            is_reachable=False,
            last_reachability_check=timezone.now(),
            last_error=str(exc),
        )
        return

    Player.objects.filter(id=player_id).update(
        is_reachable=True,
        mac_address=info.get('mac_address', player.mac_address),
        device_model=info.get('device_model', player.device_model),
        anthias_version=info.get('anthias_version', player.anthias_version),
        last_reachability_check=timezone.now(),
        last_error='',
    )


@celery.task(
    soft_time_limit=HEARTBEAT_SWEEP_SOFT_TIME_LIMIT_S,
    time_limit=HEARTBEAT_SWEEP_TIME_LIMIT_S,
)
def heartbeat_sweep() -> None:
    """Fan out one ``poll_player_heartbeat`` task per registered player.

    Dispatches via ``.delay()`` rather than polling players
    synchronously in-process — an in-process loop would serialize N
    unreachable players' worth of connect timeouts behind each other
    inside a single task, turning one flaky player into a multi-second
    stall for every other player's heartbeat in the same sweep.
    """
    from anthias_server.fleet.models import Player

    for player_id in Player.objects.values_list('id', flat=True):
        poll_player_heartbeat.delay(player_id)
