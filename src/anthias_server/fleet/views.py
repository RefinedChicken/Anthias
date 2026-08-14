"""Views for the fleet-management console (``ANTHIAS_SERVICE=fleet``).

Thin-orchestrator design: every write here proxies to the *player's*
own v2 REST API via ``PlayerAPIClient`` — this process never touches a
player's Asset table directly, only ``fleet.models.Player`` /
``PlayerGroup`` rows in its own DB. See ``anthias_server.fleet
.player_client`` for the client and ``anthias_server.fleet.adapters``
for how a player's JSON asset payload becomes a renderable (transient,
unsaved) ``Asset`` for the drill-down templates.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from mimetypes import guess_type
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.conf import settings as django_settings
from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from anthias_server.app.models import clamp_refresh_interval
from anthias_server.app.views import (
    _checkbox,
    _host_allowed,
    _set_toast_header,
)
from anthias_server.fleet.adapters import asset_from_player_dict
from anthias_server.fleet.helpers import template
from anthias_server.fleet.models import Player, PlayerGroup
from anthias_server.fleet.player_client import (
    PlayerAPIClient,
    PlayerAPIError,
    PlayerUnreachableError,
)
from anthias_server.lib.auth import authorized

if TYPE_CHECKING:
    from anthias_server.app.models import Asset

logger = logging.getLogger(__name__)

_url_validator = URLValidator(schemes=('http', 'https'))


def _client_error_message(exc: Exception) -> str:
    """Operator-facing text for the two ``PlayerAPIClient`` error
    types, shared by every proxied write/read view below so the
    phrasing stays consistent."""
    if isinstance(exc, PlayerAPIError):
        return f'Player returned an error ({exc.status_code}): {exc.message}'
    return f'Could not reach this player: {exc}'


def _pluralize(count: int, suffix: str = 's') -> str:
    return '' if count == 1 else suffix


def _bulk_ids(request: HttpRequest) -> list[str]:
    """Comma-separated selection posted by a bulk-action bar. Mirrors
    ``anthias_server.app.views._bulk_ids``' contract (strip + drop
    empties) without importing that module's private helper — this one
    isn't wire-format-sensitive like the HX-Trigger toast helpers are,
    so a small local copy keeps this module's dependency on ``app
    .views`` to just the two functions that must match byte-for-byte.
    """
    parts = (i.strip() for i in request.POST.get('ids', '').split(','))
    return [i for i in parts if i]


# ---------------------------------------------------------------------------
# Players — list / registration / drill-down


def _player_list_context() -> dict[str, Any]:
    players = list(Player.objects.select_related('group').order_by('name'))
    total = len(players)
    online = sum(1 for p in players if p.is_reachable)
    return {
        'players': players,
        'groups': PlayerGroup.objects.all(),
        'total_players': total,
        'online_players': online,
        'offline_players': total - online,
        'active_nav': 'players',
    }


@authorized
@require_http_methods(['GET'])
def player_list(request: HttpRequest) -> HttpResponse:
    return template(request, 'fleet/player_list.html', _player_list_context())


@authorized
@require_http_methods(['GET'])
def player_table_partial(request: HttpRequest) -> HttpResponse:
    """HTMX endpoint for the player table only. Reads local DB state —
    ``Player.is_reachable``/``last_reachability_check``/etc. are kept
    fresh by the Celery heartbeat poller (a separate piece of work);
    this view never calls out to a player over HTTP itself, so a ~10-15s
    poll interval here is cheap."""
    return render(request, 'fleet/_player_table.html', _player_list_context())


@authorized
@require_http_methods(['GET', 'POST'])
def player_new(request: HttpRequest) -> HttpResponse:
    groups = PlayerGroup.objects.all()
    if request.method == 'GET':
        return template(request, 'fleet/player_new.html', {'groups': groups})

    name = (request.POST.get('name') or '').strip()
    base_url = (request.POST.get('base_url') or '').strip()
    api_token = (request.POST.get('api_token') or '').strip()
    skip_ssl_verify = request.POST.get('skip_ssl_verify') == 'true'
    group_id = request.POST.get('group') or ''

    def _redisplay() -> HttpResponse:
        return template(
            request,
            'fleet/player_new.html',
            {'groups': groups, 'form_values': request.POST},
        )

    if not name or not base_url or not api_token:
        messages.error(
            request, 'Name, base URL, and API token are all required.'
        )
        return _redisplay()
    try:
        _url_validator(base_url)
    except DjangoValidationError:
        messages.error(request, f'"{base_url}" is not a valid URL.')
        return _redisplay()

    # Validate synchronously against an unsaved candidate row before
    # persisting anything — a fleet console shouldn't accumulate rows
    # for players it already knows it can't talk to.
    candidate = Player(
        name=name, base_url=base_url, skip_ssl_verify=skip_ssl_verify
    )
    candidate.set_api_token(api_token)

    try:
        info = PlayerAPIClient(candidate).get_info()
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        messages.error(request, _client_error_message(exc))
        return _redisplay()

    candidate.mac_address = info.get('mac_address') or ''
    candidate.device_model = info.get('device_model') or ''
    candidate.anthias_version = info.get('anthias_version') or ''
    candidate.is_reachable = True
    candidate.last_reachability_check = timezone.now()
    if group_id:
        candidate.group = PlayerGroup.objects.filter(pk=group_id).first()
    candidate.save()

    messages.success(request, f'Registered player "{name}".')
    return redirect(reverse('anthias_fleet:player_list'))


@authorized
@require_http_methods(['GET', 'POST'])
def player_edit(request: HttpRequest, player_id: int) -> HttpResponse:
    player = get_object_or_404(Player, pk=player_id)
    groups = PlayerGroup.objects.all()
    if request.method == 'GET':
        return template(
            request,
            'fleet/player_edit.html',
            {'player': player, 'groups': groups},
        )

    name = (request.POST.get('name') or '').strip()
    base_url = (request.POST.get('base_url') or '').strip()
    api_token = (request.POST.get('api_token') or '').strip()
    skip_ssl_verify = request.POST.get('skip_ssl_verify') == 'true'
    group_id = request.POST.get('group') or ''

    if not name or not base_url:
        messages.error(request, 'Name and base URL are required.')
        return template(
            request,
            'fleet/player_edit.html',
            {'player': player, 'groups': groups},
        )
    try:
        _url_validator(base_url)
    except DjangoValidationError:
        messages.error(request, f'"{base_url}" is not a valid URL.')
        return template(
            request,
            'fleet/player_edit.html',
            {'player': player, 'groups': groups},
        )

    player.name = name
    player.base_url = base_url
    player.skip_ssl_verify = skip_ssl_verify
    player.group = (
        PlayerGroup.objects.filter(pk=group_id).first() if group_id else None
    )
    # Blank token field means "keep the existing one" — an operator
    # correcting the base_url/group/name shouldn't have to re-paste a
    # still-valid token.
    if api_token:
        player.set_api_token(api_token)
    player.save()

    messages.success(request, f'Updated "{name}".')
    return redirect(reverse('anthias_fleet:player_detail', args=[player.pk]))


@authorized
@require_http_methods(['POST'])
def player_delete(request: HttpRequest, player_id: int) -> HttpResponse:
    player = get_object_or_404(Player, pk=player_id)
    name = player.name
    player.delete()
    messages.success(request, f'Removed "{name}" from the fleet.')
    return redirect(reverse('anthias_fleet:player_list'))


_PLAYER_BULK_ACTIONS: dict[str, str] = {
    'reboot': 'rebooted',
    'shutdown': 'shut down',
    'display_on': 'display turned on',
    'display_off': 'display turned off',
}


@authorized
@require_http_methods(['POST'])
def players_bulk_action(request: HttpRequest) -> HttpResponse:
    """Fleet-wide reboot / shutdown / display-power across a
    multi-selected set of players. Loops one ``PlayerAPIClient`` call
    per selected player — a single unreachable device must not stop
    the rest, and the per-player outcome is reported back rather than
    swallowed."""
    action = request.POST.get('action', '')
    ids = _bulk_ids(request)

    if action not in _PLAYER_BULK_ACTIONS:
        messages.error(request, 'Unknown bulk action.')
        return redirect(reverse('anthias_fleet:player_list'))
    if not ids:
        messages.info(request, 'No players selected.')
        return redirect(reverse('anthias_fleet:player_list'))

    succeeded: list[str] = []
    failed: list[str] = []
    for player in Player.objects.filter(pk__in=ids):
        client = PlayerAPIClient(player)
        try:
            if action == 'reboot':
                client.reboot()
            elif action == 'shutdown':
                client.shutdown()
            elif action == 'display_on':
                client.set_display_power('on')
            else:
                client.set_display_power('off')
            succeeded.append(player.name)
        except (PlayerUnreachableError, PlayerAPIError) as exc:
            failed.append(f'{player.name} ({_client_error_message(exc)})')

    verb = _PLAYER_BULK_ACTIONS[action]
    if succeeded:
        messages.success(
            request,
            f'{len(succeeded)} player{_pluralize(len(succeeded))} {verb}: '
            + ', '.join(succeeded),
        )
    if failed:
        messages.error(
            request,
            f'{len(failed)} player{_pluralize(len(failed))} failed: '
            + '; '.join(failed),
        )
    return redirect(reverse('anthias_fleet:player_list'))


# ---------------------------------------------------------------------------
# Player drill-down — live asset table proxied from the player itself


def _fetch_player_assets(
    player: Player,
) -> tuple[list[Asset], str | None]:
    client = PlayerAPIClient(player)
    try:
        raw_assets = client.list_assets()
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return [], _client_error_message(exc)
    return [asset_from_player_dict(a) for a in raw_assets], None


def _player_detail_context(player: Player) -> dict[str, Any]:
    assets, fetch_error = _fetch_player_assets(player)
    active = sorted(
        (a for a in assets if a.is_enabled and not a.is_processing),
        key=lambda a: a.play_order,
    )
    inactive = [
        a for a in assets if not (a.is_enabled and not a.is_processing)
    ]
    return {
        'player': player,
        'active_assets': active,
        'inactive_assets': inactive,
        'fetch_error': fetch_error,
        # Store-catalog index URL for the Add → Apps tab (read
        # client-side off a <meta> tag in base.html). fleet.helpers
        # .template() deliberately doesn't inject this the way
        # app.helpers.template() does for every player page — the
        # drill-down is the one fleet page whose Add modal actually
        # has an Apps tab, so it's set here instead.
        'app_store_index_url': django_settings.APP_STORE_INDEX_URL,
        'active_nav': 'players',
    }


@authorized
@require_http_methods(['GET'])
def player_detail(request: HttpRequest, player_id: int) -> HttpResponse:
    player = get_object_or_404(Player, pk=player_id)
    return template(
        request, 'fleet/player_detail.html', _player_detail_context(player)
    )


# Fields proxied to/from a player's own v2/device_settings — deliberately
# the same set settings.html locks locally when a player is
# fleet-managed (see that template's "Fleet Management" section and
# app.views.settings_save's docstring). auth_backend/username/password
# are NOT in this set: user-account management always stays local per
# the confirmed design decision, so this panel never touches it.
_PLAYER_SETTINGS_TEXT_FIELDS = (
    'player_name',
    'audio_output',
    'date_format',
    'timezone',
)
_PLAYER_SETTINGS_INT_FIELDS = (
    'default_duration',
    'default_streaming_duration',
    'screen_rotation',
)
_PLAYER_SETTINGS_BOOL_FIELDS = (
    'show_splash',
    'default_assets',
    'shuffle_playlist',
    'use_24_hour_clock',
    'debug_logging',
    'prefer_dark_mode',
    'verify_ssl',
)


@authorized
@require_http_methods(['GET', 'POST'])
def player_settings(request: HttpRequest, player_id: int) -> HttpResponse:
    """Remote equivalent of the player's own Display & Playback /
    Player Identity settings — what a fleet-managed player's local
    Settings page points operators here for instead. Proxies straight
    to that player's own v2/device_settings; there's no local copy of
    these values in the fleet DB to drift out of sync."""
    player = get_object_or_404(Player, pk=player_id)
    client = PlayerAPIClient(player)

    if request.method == 'POST':
        data: dict[str, Any] = {}
        for field in _PLAYER_SETTINGS_TEXT_FIELDS:
            if field in request.POST:
                data[field] = request.POST.get(field, '')
        for field in _PLAYER_SETTINGS_INT_FIELDS:
            raw = request.POST.get(field)
            if raw is not None and raw != '':
                try:
                    data[field] = int(raw)
                except ValueError:
                    messages.error(request, f'"{field}" must be a number.')
                    return redirect(
                        reverse(
                            'anthias_fleet:player_settings', args=[player.pk]
                        )
                    )
        for field in _PLAYER_SETTINGS_BOOL_FIELDS:
            data[field] = _checkbox(request, field)

        try:
            client.update_device_settings(data)
        except (PlayerUnreachableError, PlayerAPIError) as exc:
            messages.error(request, _client_error_message(exc))
        else:
            messages.success(request, f'Updated settings for "{player.name}".')
        return redirect(
            reverse('anthias_fleet:player_settings', args=[player.pk])
        )

    try:
        device_settings = client.get_device_settings()
        fetch_error: str | None = None
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        device_settings = {}
        fetch_error = _client_error_message(exc)

    return template(
        request,
        'fleet/player_settings.html',
        {
            'player': player,
            'device_settings': device_settings,
            'fetch_error': fetch_error,
            'active_nav': 'players',
        },
    )


def _player_asset_table_response(
    request: HttpRequest,
    player: Player,
    *,
    toast: tuple[str, str] | None = None,
) -> HttpResponse:
    """Shared response helper for every proxied asset write below —
    mirrors ``anthias_server.app.views._asset_table_response``'s
    htmx-partial-or-redirect contract, reusing its exact HX-Trigger
    toast wire format (``_set_toast_header``) so the same client-side
    toast listener in vendor.ts drives both surfaces without a second
    implementation to keep in sync."""
    context = _player_detail_context(player)

    if request.headers.get('HX-Request'):
        response = render(request, 'fleet/_player_asset_table.html', context)
        if toast is not None:
            _set_toast_header(response, toast[0], toast[1])
        return response

    if toast is not None:
        _msg_fn = {
            'success': messages.success,
            'error': messages.error,
            'info': messages.info,
        }.get(toast[0], messages.info)
        _msg_fn(request, toast[1])
    return redirect(reverse('anthias_fleet:player_detail', args=[player.pk]))


@authorized
@require_http_methods(['GET'])
def player_assets_table_partial(
    request: HttpRequest, player_id: int
) -> HttpResponse:
    player = get_object_or_404(Player, pk=player_id)
    return render(
        request,
        'fleet/_player_asset_table.html',
        _player_detail_context(player),
    )


@authorized
@require_http_methods(['POST'])
def player_asset_create(request: HttpRequest, player_id: int) -> HttpResponse:
    """Minimal URI-based add (name/uri/mimetype/duration) — the
    fleet-console equivalent of the player app's URL-tab add flow.
    File upload (``player_asset_upload``) and app-store installs
    (``player_asset_create_app``) are proxied by their own views below.
    YouTube URL detection is still local-only — an operator who needs
    that pastes into the player's own UI directly."""
    player = get_object_or_404(Player, pk=player_id)

    uri = (request.POST.get('uri') or '').strip()
    if not uri:
        return _player_asset_table_response(
            request, player, toast=('error', 'A URI is required.')
        )
    name = (request.POST.get('name') or '').strip() or uri
    mimetype = request.POST.get('mimetype') or 'webpage'
    if mimetype not in ('image', 'video', 'webpage'):
        mimetype = 'webpage'
    try:
        duration = int(request.POST.get('duration') or 10)
    except (TypeError, ValueError):
        duration = 10

    now = timezone.now()
    data = {
        'name': name,
        'uri': uri,
        'mimetype': mimetype,
        'duration': max(0, duration),
        'is_enabled': True,
        'start_date': now.isoformat(),
        'end_date': (now + timedelta(days=30)).isoformat(),
    }

    client = PlayerAPIClient(player)
    try:
        client.create_asset(data)
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )
    return _player_asset_table_response(
        request, player, toast=('success', 'Asset added')
    )


@authorized
@require_http_methods(['POST'])
def player_asset_upload(request: HttpRequest, player_id: int) -> HttpResponse:
    """File-upload tab equivalent for the drill-down. Unlike the
    player's own ``assets_upload`` (which writes straight into the
    local ``assetdir`` and creates the ``Asset`` row via the ORM), this
    is a pure network proxy: the file is forwarded to the player's own
    ``v2/file_asset`` endpoint, and the on-player path it hands back is
    then passed to ``create_asset()`` — the same two-step
    upload-then-create sequence a third-party API client would use, and
    the same one the player's own upload pipeline follows internally
    (``CreateAssetSerializerMixin.prepare_asset``'s ``is_local_upload``
    branch)."""
    player = get_object_or_404(Player, pk=player_id)

    file_upload = request.FILES.get('file_upload')
    if file_upload is None or not file_upload.name:
        return _player_asset_table_response(
            request, player, toast=('error', 'No file uploaded.')
        )
    upload_name: str = file_upload.name

    # Matches the player's own gate (FileAssetViewMixin.post): image/*
    # or video/* only, decided from the filename — checked here first
    # so a rejected file doesn't cost a round trip to the player.
    file_type = guess_type(upload_name)[0] or ''
    if file_type.split('/')[0] not in ('image', 'video'):
        return _player_asset_table_response(
            request,
            player,
            toast=('error', 'Invalid file type. Expected image or video.'),
        )
    mimetype = file_type.split('/')[0]

    name = (request.POST.get('name') or '').strip() or upload_name
    try:
        duration = int(request.POST.get('duration') or 10)
    except (TypeError, ValueError):
        duration = 10

    client = PlayerAPIClient(player)
    try:
        uploaded = client.upload_file(
            upload_name, file_upload.read(), file_type
        )
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )

    now = timezone.now()
    # Video duration must be zero on create — the player's own
    # normalisation pipeline (ffprobe) fills in the real value once the
    # upload is processed, mirroring the v2 API's own rule (see
    # CreateAssetSerializerMixin.prepare_asset).
    data: dict[str, Any] = {
        'name': name,
        'uri': uploaded['uri'],
        'mimetype': mimetype,
        'duration': 0 if mimetype == 'video' else max(0, duration),
        'is_enabled': True,
        'start_date': now.isoformat(),
        'end_date': (now + timedelta(days=30)).isoformat(),
    }
    if uploaded.get('ext'):
        data['ext'] = uploaded['ext']

    try:
        client.create_asset(data)
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )
    return _player_asset_table_response(
        request, player, toast=('success', f'Uploaded {upload_name}')
    )


@authorized
@require_http_methods(['POST'])
def player_asset_create_app(
    request: HttpRequest, player_id: int
) -> HttpResponse:
    """Apps-tab equivalent for the drill-down. By the time this is
    called the operator's browser has already resolved the app's
    launch URL / manifest / config values (the fleet drill-down loads
    the exact same catalog-browsing JS the player app's own Add modal
    uses) — this just forwards that same POST shape to
    ``create_asset()`` instead of the local ORM, mirroring
    ``anthias_server.app.views.assets_create_app``.

    Note: the v2 API's create serializer only accepts a handful of
    declared fields — unlike the player's own local ORM path,
    arbitrary ``metadata`` (including the ``metadata.app`` stamp that
    lets the player's Edit modal reopen the app's config form) isn't
    settable through it and is silently dropped. The asset still gets
    created and plays correctly as a plain webpage; it just won't be
    editable as an "app" from the player's own UI afterwards. Only
    ``refresh_interval_s`` round-trips, since that one is a declared
    field on the create serializer.
    """
    player = get_object_or_404(Player, pk=player_id)

    # Posted as app_uri / app_values (not uri / values) so the Apps
    # form's hidden inputs never collide with the URL tab's visible
    # name="uri" input — matches assets_create_app's own convention.
    uri = (request.POST.get('app_uri') or '').strip()
    app_id = (request.POST.get('app_id') or '').strip()
    manifest_url = (request.POST.get('manifest_url') or '').strip()
    manifest_version = (request.POST.get('manifest_version') or '').strip()
    name = (request.POST.get('name') or '').strip()

    if not app_id or not uri:
        return _player_asset_table_response(
            request,
            player,
            toast=('error', 'Could not add app — invalid app data.'),
        )
    try:
        _url_validator(uri)
    except DjangoValidationError:
        return _player_asset_table_response(
            request,
            player,
            toast=('error', 'Could not add app — invalid app data.'),
        )

    # Defence in depth, mirroring assets_create_app: the launch URL and
    # manifest must sit on an allowed store origin, so this endpoint
    # can't be used to stamp arbitrary URLs as store apps.
    if not _host_allowed(urlparse(uri).hostname or ''):
        return _player_asset_table_response(
            request,
            player,
            toast=('error', 'That app is not from a recognised app store.'),
        )
    if manifest_url and not _host_allowed(
        urlparse(manifest_url).hostname or ''
    ):
        return _player_asset_table_response(
            request,
            player,
            toast=('error', 'That app is not from a recognised app store.'),
        )

    # Setting values chosen in the config form, echoed back into
    # metadata.app for the edit-reopen flow — see the docstring caveat
    # above about the v2 API dropping this key today.
    try:
        values = json.loads(request.POST.get('app_values') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        values = {}
    if not isinstance(values, dict):
        values = {}

    now = timezone.now()
    data: dict[str, Any] = {
        'name': name or app_id,
        'uri': uri,
        'mimetype': 'webpage',
        'duration': 10,
        'is_enabled': True,
        'start_date': now.isoformat(),
        'end_date': (now + timedelta(days=30)).isoformat(),
        'metadata': {
            'app': {
                'id': app_id,
                'manifest_url': manifest_url,
                'manifest_version': manifest_version,
                'values': values,
            }
        },
    }

    raw_interval = request.POST.get('refresh_interval_s')
    if raw_interval is not None and raw_interval.strip():
        interval = clamp_refresh_interval(raw_interval.strip())
        if interval:
            data['refresh_interval_s'] = interval

    client = PlayerAPIClient(player)
    try:
        client.create_asset(data)
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )
    return _player_asset_table_response(
        request, player, toast=('success', 'App added')
    )


@authorized
@require_http_methods(['POST'])
def player_asset_toggle(
    request: HttpRequest, player_id: int, asset_id: str
) -> HttpResponse:
    """Flip enabled/disabled. The row's hidden ``is_enabled`` input
    already carries the *target* state (see ``_player_asset_row.html``),
    so this is a single PATCH — no read-then-write round trip needed."""
    player = get_object_or_404(Player, pk=player_id)
    target = request.POST.get('is_enabled') == 'true'

    client = PlayerAPIClient(player)
    try:
        client.update_asset(asset_id, {'is_enabled': target})
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )
    verb = 'enabled' if target else 'disabled'
    return _player_asset_table_response(
        request, player, toast=('success', f'Asset {verb}')
    )


@authorized
@require_http_methods(['POST'])
def player_asset_delete(
    request: HttpRequest, player_id: int, asset_id: str
) -> HttpResponse:
    player = get_object_or_404(Player, pk=player_id)
    client = PlayerAPIClient(player)
    try:
        client.delete_asset(asset_id)
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )
    return _player_asset_table_response(
        request, player, toast=('success', 'Asset deleted')
    )


@authorized
@require_http_methods(['POST'])
def player_asset_move(
    request: HttpRequest, player_id: int, asset_id: str, direction: str
) -> HttpResponse:
    """Up/down reordering, the deliberately-simpler MVP substitute for
    porting home.ts's hand-rolled pointer-based drag-reorder gesture
    (~lines 634-691) — not worth the complexity here. Moves ``asset_id``
    one slot within the *active* ordering and pushes the whole new
    order back via ``reorder_assets`` (same wire shape the player app's
    own drag handler uses)."""
    if direction not in ('up', 'down'):
        raise Http404('Unknown move direction.')
    player = get_object_or_404(Player, pk=player_id)

    client = PlayerAPIClient(player)
    try:
        raw_assets = client.list_assets()
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )

    active = sorted(
        (
            a
            for a in raw_assets
            if a.get('is_enabled') and not a.get('is_processing')
        ),
        key=lambda a: a.get('play_order') or 0,
    )
    ids = [a['asset_id'] for a in active]
    if asset_id not in ids:
        return _player_asset_table_response(
            request,
            player,
            toast=('info', 'Asset is no longer in the active list'),
        )

    idx = ids.index(asset_id)
    swap_with = idx - 1 if direction == 'up' else idx + 1
    if not (0 <= swap_with < len(ids)):
        # Already at the top/bottom — nothing to do, but still a
        # normal outcome, not an error.
        return _player_asset_table_response(request, player)

    ids[idx], ids[swap_with] = ids[swap_with], ids[idx]
    try:
        client.reorder_assets(ids)
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        return _player_asset_table_response(
            request, player, toast=('error', _client_error_message(exc))
        )
    return _player_asset_table_response(
        request, player, toast=('success', 'Order updated')
    )


@authorized
@require_http_methods(['POST'])
def player_asset_control(
    request: HttpRequest, player_id: int, command: str
) -> HttpResponse:
    """Previous / Next playback on this one player."""
    if command not in ('previous', 'next'):
        raise Http404('Unknown control command.')
    player = get_object_or_404(Player, pk=player_id)
    client = PlayerAPIClient(player)
    try:
        client.control(command)
    except (PlayerUnreachableError, PlayerAPIError) as exc:
        messages.error(request, _client_error_message(exc))
    return redirect(reverse('anthias_fleet:player_detail', args=[player.pk]))


@authorized
@require_http_methods(['POST'])
def player_assets_bulk_action(
    request: HttpRequest, player_id: int
) -> HttpResponse:
    """Enable / disable / delete a set of this player's assets in one
    request. Loops one ``PlayerAPIClient`` call per selected asset —
    same partial-failure posture as ``players_bulk_action``: the
    summary always names how many succeeded vs. failed rather than
    reporting a blanket success."""
    player = get_object_or_404(Player, pk=player_id)
    action = request.POST.get('action', '')
    ids = _bulk_ids(request)

    if action not in ('enable', 'disable', 'delete'):
        return _player_asset_table_response(
            request, player, toast=('error', 'Unknown bulk action')
        )
    if not ids:
        return _player_asset_table_response(
            request, player, toast=('info', 'No assets selected')
        )

    client = PlayerAPIClient(player)
    succeeded = 0
    failures: list[str] = []
    for asset_id in ids:
        try:
            if action == 'delete':
                client.delete_asset(asset_id)
            else:
                client.update_asset(
                    asset_id, {'is_enabled': action == 'enable'}
                )
            succeeded += 1
        except (PlayerUnreachableError, PlayerAPIError) as exc:
            failures.append(f'{asset_id}: {_client_error_message(exc)}')

    verb = {'enable': 'enabled', 'disable': 'disabled', 'delete': 'deleted'}[
        action
    ]
    if not succeeded:
        return _player_asset_table_response(
            request,
            player,
            toast=('error', 'All actions failed: ' + '; '.join(failures[:3])),
        )
    msg = f'{succeeded} asset{_pluralize(succeeded)} {verb}'
    if failures:
        msg += f'; {len(failures)} failed'
    return _player_asset_table_response(
        request, player, toast=('info' if failures else 'success', msg)
    )


# ---------------------------------------------------------------------------
# Player groups — simple CRUD


@authorized
@require_http_methods(['GET'])
def group_list(request: HttpRequest) -> HttpResponse:
    groups = PlayerGroup.objects.annotate(player_count=Count('players'))
    return template(
        request,
        'fleet/group_list.html',
        {'groups': groups, 'active_nav': 'groups'},
    )


@authorized
@require_http_methods(['GET', 'POST'])
def group_new(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        return template(
            request, 'fleet/group_form.html', {'active_nav': 'groups'}
        )

    name = (request.POST.get('name') or '').strip()
    description = (request.POST.get('description') or '').strip()
    if not name:
        messages.error(request, 'Group name is required.')
        return template(
            request,
            'fleet/group_form.html',
            {'form_values': request.POST, 'active_nav': 'groups'},
        )
    if PlayerGroup.objects.filter(name=name).exists():
        messages.error(request, f'A group named "{name}" already exists.')
        return template(
            request,
            'fleet/group_form.html',
            {'form_values': request.POST, 'active_nav': 'groups'},
        )

    PlayerGroup.objects.create(name=name, description=description)
    messages.success(request, f'Created group "{name}".')
    return redirect(reverse('anthias_fleet:group_list'))


@authorized
@require_http_methods(['GET', 'POST'])
def group_edit(request: HttpRequest, group_id: int) -> HttpResponse:
    group = get_object_or_404(PlayerGroup, pk=group_id)
    if request.method == 'GET':
        return template(
            request,
            'fleet/group_form.html',
            {'group': group, 'active_nav': 'groups'},
        )

    name = (request.POST.get('name') or '').strip()
    description = (request.POST.get('description') or '').strip()
    if not name:
        messages.error(request, 'Group name is required.')
        return template(
            request,
            'fleet/group_form.html',
            {'group': group, 'active_nav': 'groups'},
        )
    if PlayerGroup.objects.exclude(pk=group.pk).filter(name=name).exists():
        messages.error(request, f'A group named "{name}" already exists.')
        return template(
            request,
            'fleet/group_form.html',
            {'group': group, 'active_nav': 'groups'},
        )

    group.name = name
    group.description = description
    group.save()
    messages.success(request, f'Updated group "{name}".')
    return redirect(reverse('anthias_fleet:group_list'))


@authorized
@require_http_methods(['POST'])
def group_delete(request: HttpRequest, group_id: int) -> HttpResponse:
    group = get_object_or_404(PlayerGroup, pk=group_id)
    name = group.name
    group.delete()
    messages.success(request, f'Deleted group "{name}".')
    return redirect(reverse('anthias_fleet:group_list'))
