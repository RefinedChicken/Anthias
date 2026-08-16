"""Tests for the new Player/Fleet-Server v2 read endpoints:
``player/identity``, ``player/policy``, ``playlists``, ``schedules``,
``management/commands``, ``management/sync-state``.

All six are read-only in this phase — nothing creates ``FleetPairing``,
``Command``, or ``SyncState`` rows yet (pairing/sync/commands are later
phases), so most of these assert the standalone/empty defaults. Where a
test seeds a row directly via the model, that's standing in for the
not-yet-built phase that will actually write it.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from anthias_server.app.models import AUTHORITY_FLEET, AUTHORITY_LOCAL, Asset
from anthias_server.fleet_link.models import (
    Command,
    FleetPairing,
    PlayerIdentity,
    SyncState,
)


def _make_asset(**kwargs: object) -> Asset:
    defaults: dict[str, object] = {'name': 'test-asset'}
    defaults.update(kwargs)
    return Asset.objects.create(**defaults)


# ---------------------------------------------------------------------------
# player/identity


@pytest.mark.django_db
def test_player_identity_is_standalone_by_default() -> None:
    response = Client().get(reverse('api:player_identity_v2'))

    assert response.status_code == 200
    body = response.json()
    assert body['management_state'] == 'standalone'
    assert body['fleet_base_url'] is None
    assert body['device_id']


@pytest.mark.django_db
def test_player_identity_device_id_is_stable_across_calls() -> None:
    client = Client()
    first = client.get(reverse('api:player_identity_v2')).json()
    second = client.get(reverse('api:player_identity_v2')).json()

    assert first['device_id'] == second['device_id']
    assert PlayerIdentity.objects.count() == 1


@pytest.mark.django_db
def test_player_identity_reflects_pending_pairing() -> None:
    FleetPairing.objects.create(
        fleet_base_url='https://fleet.example.com',
        status=FleetPairing.PENDING,
    )

    body = Client().get(reverse('api:player_identity_v2')).json()

    assert body['management_state'] == 'pending'
    assert body['fleet_base_url'] == 'https://fleet.example.com'


@pytest.mark.django_db
def test_player_identity_reflects_active_pairing_as_paired() -> None:
    FleetPairing.objects.create(
        fleet_base_url='https://fleet.example.com',
        status=FleetPairing.ACTIVE,
    )

    body = Client().get(reverse('api:player_identity_v2')).json()

    assert body['management_state'] == 'paired'


@pytest.mark.django_db
def test_player_identity_ignores_revoked_pairing() -> None:
    FleetPairing.objects.create(
        fleet_base_url='https://old-fleet.example.com',
        status=FleetPairing.REVOKED,
    )

    body = Client().get(reverse('api:player_identity_v2')).json()

    assert body['management_state'] == 'standalone'


# ---------------------------------------------------------------------------
# player/policy


@pytest.mark.django_db
def test_player_policy_is_all_local_by_default() -> None:
    body = Client().get(reverse('api:player_policy_v2')).json()

    assert body == {
        'content_authority': AUTHORITY_LOCAL,
        'playlist_authority': AUTHORITY_LOCAL,
        'schedule_authority': AUTHORITY_LOCAL,
        'config_authority': AUTHORITY_LOCAL,
    }


@pytest.mark.django_db
def test_player_policy_reflects_active_pairings_per_domain_authority() -> None:
    FleetPairing.objects.create(
        fleet_base_url='https://fleet.example.com',
        status=FleetPairing.ACTIVE,
        content_authority=AUTHORITY_FLEET,
        playlist_authority=AUTHORITY_FLEET,
        # schedule_authority / config_authority left at their 'local' default
    )

    body = Client().get(reverse('api:player_policy_v2')).json()

    assert body['content_authority'] == AUTHORITY_FLEET
    assert body['playlist_authority'] == AUTHORITY_FLEET
    assert body['schedule_authority'] == AUTHORITY_LOCAL
    assert body['config_authority'] == AUTHORITY_LOCAL


# ---------------------------------------------------------------------------
# playlists


@pytest.mark.django_db
def test_playlists_returns_a_paginated_envelope() -> None:
    body = Client().get(reverse('api:playlist_list_v2')).json()

    assert set(body) == {'count', 'next', 'previous', 'results'}
    assert body['count'] == 0
    assert body['results'] == []


@pytest.mark.django_db
def test_playlists_orders_by_play_order_and_reports_authority() -> None:
    _make_asset(asset_id='second', name='second', play_order=2)
    _make_asset(
        asset_id='first',
        name='first',
        play_order=1,
        origin=AUTHORITY_FLEET,
        authority=AUTHORITY_FLEET,
    )

    body = Client().get(reverse('api:playlist_list_v2')).json()

    ids = [item['asset_id'] for item in body['results']]
    assert ids == ['first', 'second']
    assert body['results'][0]['authority'] == AUTHORITY_FLEET
    assert body['results'][0]['origin'] == AUTHORITY_FLEET
    assert body['results'][1]['authority'] == AUTHORITY_LOCAL


# ---------------------------------------------------------------------------
# schedules


@pytest.mark.django_db
def test_schedules_reports_play_days_and_window() -> None:
    _make_asset(
        asset_id='scheduled',
        name='scheduled',
        play_days='[1, 3, 5]',
    )

    body = Client().get(reverse('api:schedule_list_v2')).json()

    assert body['count'] == 1
    row = body['results'][0]
    assert row['asset_id'] == 'scheduled'
    assert row['play_days'] == [1, 3, 5]
    assert row['authority'] == AUTHORITY_LOCAL


# ---------------------------------------------------------------------------
# management/commands


@pytest.mark.django_db
def test_management_commands_is_empty_by_default() -> None:
    body = Client().get(reverse('api:management_commands_v2')).json()

    assert body['count'] == 0
    assert body['results'] == []


@pytest.mark.django_db
def test_management_commands_lists_existing_rows_most_recent_first() -> None:
    Command.objects.create(command_id='cmd-1', type=Command.REBOOT)
    Command.objects.create(command_id='cmd-2', type=Command.TAKE_SCREENSHOT)

    body = Client().get(reverse('api:management_commands_v2')).json()

    assert [row['command_id'] for row in body['results']] == [
        'cmd-2',
        'cmd-1',
    ]


# ---------------------------------------------------------------------------
# management/sync-state


@pytest.mark.django_db
def test_management_sync_state_is_empty_by_default() -> None:
    body = Client().get(reverse('api:management_sync_state_v2')).json()

    assert body['count'] == 0
    assert body['results'] == []


@pytest.mark.django_db
def test_management_sync_state_lists_existing_rows_by_domain() -> None:
    SyncState.objects.create(domain=SyncState.MEDIA, desired_version=3)
    SyncState.objects.create(domain=SyncState.CONFIG, applied_version=1)

    body = Client().get(reverse('api:management_sync_state_v2')).json()

    domains = [row['domain'] for row in body['results']]
    assert domains == ['config', 'media']
