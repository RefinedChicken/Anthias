"""Celery tasks for the fleet server: heartbeat polling and bulk asset
push.

Deliberately its own module rather than living in the shared
``celery_tasks.py`` (see that file's docstring/comments on Django
import-order): ``anthias_server.fleet`` is only in ``INSTALLED_APPS``
when running as the fleet service (or under the test runner), so an
unconditional top-level import of ``anthias_server.fleet.models`` from
``celery_tasks.py`` — which is imported by *every* celery worker,
player and fleet alike — would crash the player's worker at import
time with a Django ``RuntimeError`` about the model's app not being
installed. Keeping these tasks in their own fleet-only module lets
``celery_tasks.py`` import them behind an ``ANTHIAS_SERVICE == 'fleet'``
guard instead.
"""

from __future__ import annotations

import logging
from base64 import b64decode
from contextlib import suppress
from datetime import timedelta
from mimetypes import guess_type
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings as django_settings
from django.utils import timezone

from anthias_server.celery_tasks import celery
from anthias_server.fleet.player_client import (
    PlayerAPIClient,
    PlayerAPIError,
    PlayerUnreachableError,
)

logger = logging.getLogger(__name__)


def _task_error_message(exc: Exception) -> str:
    """Same phrasing as fleet.views._client_error_message, duplicated
    rather than imported — this module deliberately stays independent
    of the web-view layer (see the module docstring's import-order
    note), and this is a small enough helper that a second copy is
    cheaper than coupling the two."""
    if isinstance(exc, PlayerAPIError):
        return f'Player returned an error ({exc.status_code}): {exc.message}'
    return f'Could not reach this player: {exc}'


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

    uptime = info.get('uptime') or {}
    Player.objects.filter(id=player_id).update(
        is_reachable=True,
        mac_address=info.get('mac_address', player.mac_address),
        device_model=info.get('device_model', player.device_model),
        anthias_version=info.get('anthias_version', player.anthias_version),
        loadavg_15min=info.get('loadavg', player.loadavg_15min),
        free_space=info.get('free_space', player.free_space),
        # display_power can legitimately be None (r.get() miss on the
        # player), which must not stomp a previously-known value —
        # `or` rather than dict.get()'s default, matching the field's
        # "'' means never observed" contract.
        display_power=info.get('display_power') or player.display_power,
        up_to_date=info.get('up_to_date', player.up_to_date),
        uptime_days=uptime.get('days', player.uptime_days),
        uptime_hours=uptime.get('hours', player.uptime_hours),
        memory=info.get('memory', player.memory),
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


# --- bulk asset push --------------------------------------------------

# Sized off _TRANSFER_TIMEOUT_S's 120s read timeout (player_client.py)
# plus slack for the base64 decode + disk write, not off the heartbeat
# budgets above — this task moves a whole asset's bytes, not a small
# JSON poll.
BULK_PUSH_DISPATCH_SOFT_TIME_LIMIT_S = 180
BULK_PUSH_DISPATCH_TIME_LIMIT_S = 240

# Per-target budget: one list_assets() call plus one upload_file()/
# create_asset() pair, each bounded by the client's own timeouts.
BULK_PUSH_TARGET_SOFT_TIME_LIMIT_S = 150
BULK_PUSH_TARGET_TIME_LIMIT_S = 200


def _fail_all_pending_targets(job: Any, error: str) -> None:
    job.targets.filter(status='pending').update(
        status='failed', error=error, completed_at=timezone.now()
    )


@celery.task(
    soft_time_limit=BULK_PUSH_DISPATCH_SOFT_TIME_LIMIT_S,
    time_limit=BULK_PUSH_DISPATCH_TIME_LIMIT_S,
)
def push_asset_to_group(job_id: int) -> None:
    """Fetch the source asset once (staging its bytes to disk if it's
    file-backed), then fan out one ``push_asset_to_player`` task per
    target row — same dispatcher/per-target split as
    ``heartbeat_sweep``/``poll_player_heartbeat``. Caches the asset's
    name/mimetype/duration/uri onto the job row so no per-target task
    ever needs to contact the source player again.
    """
    from anthias_server.fleet.models import AssetPushJob

    try:
        job = AssetPushJob.objects.select_related('source_player').get(
            id=job_id
        )
    except AssetPushJob.DoesNotExist:
        return

    if job.source_player is None:
        _fail_all_pending_targets(
            job, 'Source player was removed before the push could start.'
        )
        return

    client = PlayerAPIClient(job.source_player)
    try:
        asset = client.get_asset(job.source_asset_id)
    except SoftTimeLimitExceeded:
        logger.warning(
            'push_asset_to_group: fetching source asset for job %s '
            'exceeded %ss; leaving targets pending for a retry',
            job_id,
            BULK_PUSH_DISPATCH_SOFT_TIME_LIMIT_S,
        )
        return
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        _fail_all_pending_targets(job, _task_error_message(exc))
        return

    mimetype = asset.get('mimetype') or 'webpage'
    job.mimetype = mimetype
    job.duration = asset.get('duration') or 10
    job.asset_name = asset.get('name') or job.asset_name

    if mimetype != 'webpage':
        try:
            content = client.get_asset_content(job.source_asset_id)
        except SoftTimeLimitExceeded:
            logger.warning(
                'push_asset_to_group: downloading source content for '
                'job %s exceeded %ss; leaving targets pending for a '
                'retry',
                job_id,
                BULK_PUSH_DISPATCH_SOFT_TIME_LIMIT_S,
            )
            return
        except (PlayerUnreachableError, PlayerAPIError) as exc:
            _fail_all_pending_targets(job, _task_error_message(exc))
            return

        if content.get('type') == 'file':
            staging_dir = Path(django_settings.FLEET_PUSH_STAGING_DIR)
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_path = staging_dir / f'{job_id}.bin'
            staged_path.write_bytes(b64decode(content['content']))
            job.staged_content_path = str(staged_path)
        else:
            job.source_uri = content.get('url', '')
    else:
        job.source_uri = asset.get('uri') or ''

    job.save(
        update_fields=[
            'mimetype',
            'duration',
            'asset_name',
            'staged_content_path',
            'source_uri',
        ]
    )

    for target_id in job.targets.values_list('id', flat=True):
        push_asset_to_player.delay(target_id)


def _cleanup_staged_file_if_job_done(job: Any) -> None:
    if (
        job.staged_content_path
        and not job.targets.filter(status='pending').exists()
    ):
        with suppress(FileNotFoundError):
            Path(job.staged_content_path).unlink()


@celery.task(
    soft_time_limit=BULK_PUSH_TARGET_SOFT_TIME_LIMIT_S,
    time_limit=BULK_PUSH_TARGET_TIME_LIMIT_S,
)
def push_asset_to_player(target_id: int) -> None:
    """Push one already-staged/URL source asset to one target player,
    update its ``AssetPushJobTarget`` row, and clean up the job's
    staged file once every target has finished.

    No-ops if the target row is gone — mirrors
    ``poll_player_heartbeat``'s "row deleted mid-flight" posture.
    """
    from anthias_server.fleet.models import AssetPushJobTarget

    try:
        target = AssetPushJobTarget.objects.select_related(
            'job', 'player'
        ).get(id=target_id)
    except AssetPushJobTarget.DoesNotExist:
        return

    job = target.job
    client = PlayerAPIClient(target.player)

    try:
        existing = client.list_assets()
    except SoftTimeLimitExceeded:
        logger.warning(
            'push_asset_to_player: listing assets on target %s '
            '(job %s) exceeded %ss; leaving pending for a retry',
            target.player_id,
            job.id,
            BULK_PUSH_TARGET_SOFT_TIME_LIMIT_S,
        )
        return
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        target.status = AssetPushJobTarget.STATUS_FAILED
        target.error = _task_error_message(exc)
        target.completed_at = timezone.now()
        target.save()
        _cleanup_staged_file_if_job_done(job)
        return

    is_file_backed = bool(job.staged_content_path)

    # Idempotency is a best-effort heuristic, not exact-match dedup —
    # no checksum exists anywhere player-side. URL-type assets dedup
    # reliably on an exact uri match; file-type assets can only dedup
    # on an exact name match, so a renamed re-push of the same file
    # will duplicate.
    already_present = (
        any(a.get('name') == job.asset_name for a in existing)
        if is_file_backed
        else any(a.get('uri') == job.source_uri for a in existing)
    )
    if already_present:
        target.status = AssetPushJobTarget.STATUS_SKIPPED
        target.completed_at = timezone.now()
        target.save()
        _cleanup_staged_file_if_job_done(job)
        return

    now = timezone.now()
    data: dict[str, Any] = {
        'name': job.asset_name,
        'mimetype': job.mimetype,
        'duration': job.duration,
        'is_enabled': True,
        'start_date': now.isoformat(),
        'end_date': (now + timedelta(days=30)).isoformat(),
    }

    try:
        if is_file_backed:
            content_bytes = Path(job.staged_content_path).read_bytes()
            # The target's own FileAssetViewMixin derives file type
            # from guess_type() on the *filename* it's given, not this
            # header — mirrors AssetContentViewMixin's own filename=
            # asset.name convention on the download side. If the
            # source asset's display name lacks a recognizable
            # extension, the target will reject it the same way a
            # direct content-download would; not a gap introduced
            # here.
            content_type = (
                guess_type(job.asset_name)[0] or 'application/octet-stream'
            )
            uploaded = client.upload_file(
                job.asset_name, content_bytes, content_type
            )
            data['uri'] = uploaded['uri']
            if uploaded.get('ext'):
                data['ext'] = uploaded['ext']
        else:
            data['uri'] = job.source_uri

        client.create_asset(data)
    except SoftTimeLimitExceeded:
        logger.warning(
            'push_asset_to_player: pushing to target %s (job %s) '
            'exceeded %ss; leaving pending for a retry',
            target.player_id,
            job.id,
            BULK_PUSH_TARGET_SOFT_TIME_LIMIT_S,
        )
        return
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        target.status = AssetPushJobTarget.STATUS_FAILED
        target.error = _task_error_message(exc)
        target.completed_at = timezone.now()
        target.save()
        _cleanup_staged_file_if_job_done(job)
        return

    target.status = AssetPushJobTarget.STATUS_SUCCESS
    target.completed_at = timezone.now()
    target.save()
    _cleanup_staged_file_if_job_done(job)
