"""Adapt a player's remote v2 asset JSON into a transient (unsaved)
``anthias_server.app.models.Asset`` instance.

The fleet server has no local ``Asset`` table of its own (assets live
entirely on the player being managed) — the drill-down page renders
directly from ``PlayerAPIClient.list_assets()`` / ``.get_asset()``.
Rather than inventing a parallel data shape for the templates, this
module builds a plain, never-``.save()``d ``Asset()`` per JSON item so
the drill-down templates can reuse ``Asset.is_active()`` /
``Asset.get_play_days()`` and the player app's
``anthias_server.app.templatetags.asset_filters`` filters
(``schedule_pills``, ``schedule_window``, ``to_json``,
``humanize_duration``) unchanged — those all read fields via plain
attribute access / ``getattr``, so a transient instance built from
JSON works exactly like one built from a DB row. ``anthias_server.app``
is unconditionally installed even under ``ANTHIAS_SERVICE=fleet`` (see
the comment in ``django_project/settings.py``), specifically so this
import — and the fleet DB's unused, empty ``assets`` table — are
available here.

The v2 ``AssetSerializerV2`` (``api/serializers/v2.py``) is what
produced this JSON on the player's side; its field set is what
``asset_from_player_dict`` expects. Temporal fields need parsing
because DRF serializes ``DateTimeField``/``TimeField`` to ISO strings,
but ``Asset``'s own fields are Python ``datetime``/``time`` objects —
assigning the raw strings would work for simple attribute reads but
crash the first time ``Asset.is_active()`` compares
``self.start_date < now`` (``str < datetime`` raises ``TypeError``).
"""

from __future__ import annotations

import json
from typing import Any

from django.utils.dateparse import parse_datetime, parse_time

from anthias_server.app.models import ALL_DAYS, Asset


def _parse_optional_datetime(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, str):
        return parse_datetime(value)
    return value


def _parse_optional_time(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, str):
        return parse_time(value)
    return value


def asset_from_player_dict(data: dict[str, Any]) -> Asset:
    """Build a transient ``Asset`` from one item of a player's
    ``GET /api/v2/assets`` (or ``/assets/<id>``) response body.

    Never touches ``Asset.objects`` — the returned instance exists only
    to drive template rendering for a remote payload and must never be
    ``.save()``d (doing so would write into the fleet server's own,
    otherwise-unused ``assets`` table).
    """
    play_days = data.get('play_days')
    if not isinstance(play_days, list) or not play_days:
        play_days = list(ALL_DAYS)
    # Asset.play_days is a TextField (JSON-encoded on the wire) — the
    # model's own get_play_days() also accepts a bare list (used by
    # some callers), but encoding here matches the field's declared
    # type and keeps this module mypy-clean.
    play_days_field = json.dumps(play_days)

    return Asset(
        asset_id=data.get('asset_id') or '',
        name=data.get('name') or '',
        uri=data.get('uri') or '',
        start_date=_parse_optional_datetime(data.get('start_date')),
        end_date=_parse_optional_datetime(data.get('end_date')),
        duration=data.get('duration') or 0,
        mimetype=data.get('mimetype') or '',
        is_enabled=bool(data.get('is_enabled')),
        is_processing=bool(data.get('is_processing')),
        nocache=bool(data.get('nocache')),
        play_order=data.get('play_order') or 0,
        skip_asset_check=bool(data.get('skip_asset_check')),
        skip_ssl_verify=bool(data.get('skip_ssl_verify')),
        play_days=play_days_field,
        play_time_from=_parse_optional_time(data.get('play_time_from')),
        play_time_to=_parse_optional_time(data.get('play_time_to')),
        is_reachable=bool(data.get('is_reachable', True)),
        last_reachability_check=_parse_optional_datetime(
            data.get('last_reachability_check')
        ),
        metadata=data.get('metadata') or {},
    )
