"""Tests for the fleet server's heartbeat Celery tasks
(``anthias_server.fleet.tasks``)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from anthias_server.fleet.models import Player
from anthias_server.fleet.player_client import (
    PlayerAPIError,
    PlayerUnreachableError,
)
from anthias_server.fleet.tasks import heartbeat_sweep, poll_player_heartbeat

_RAW_TOKEN = 'ant_fixture-task-token'  # NOSONAR


def _make_player(**overrides: Any) -> Player:
    player = Player(
        name=overrides.pop('name', 'Lobby TV'),
        base_url=overrides.pop('base_url', 'http://192.168.1.50:8080'),
    )
    player.set_api_token(_RAW_TOKEN)
    player.save()
    return player


@pytest.mark.django_db
def test_poll_player_heartbeat_updates_fields_on_success() -> None:
    player = _make_player()
    info = {
        'anthias_version': 'v2026.8.0',
        'mac_address': 'aa:bb:cc:dd:ee:ff',
        'device_model': 'Raspberry Pi 4',
    }

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.return_value = info
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.is_reachable is True
    assert player.mac_address == 'aa:bb:cc:dd:ee:ff'
    assert player.device_model == 'Raspberry Pi 4'
    assert player.anthias_version == 'v2026.8.0'
    assert player.last_reachability_check is not None
    assert player.last_error == ''


@pytest.mark.django_db
def test_poll_player_heartbeat_marks_unreachable_on_network_failure() -> None:
    player = _make_player()

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.side_effect = (
            PlayerUnreachableError('connection refused')
        )
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.is_reachable is False
    assert player.last_reachability_check is not None
    assert 'connection refused' in player.last_error


@pytest.mark.django_db
def test_poll_player_heartbeat_marks_unreachable_on_api_error() -> None:
    player = _make_player()

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.side_effect = PlayerAPIError(
            503, 'Service Unavailable'
        )
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.is_reachable is False
    assert player.last_reachability_check is not None
    assert '503' in player.last_error


@pytest.mark.django_db
def test_poll_player_heartbeat_noops_for_deleted_player() -> None:
    # No Player row with this id exists — must not raise.
    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        poll_player_heartbeat(999999)

    mock_client_cls.assert_not_called()


@pytest.mark.django_db
def test_heartbeat_sweep_fans_out_one_delay_call_per_player() -> None:
    player_a = _make_player(name='A')
    player_b = _make_player(name='B')

    with patch(
        'anthias_server.fleet.tasks.poll_player_heartbeat.delay'
    ) as mock_delay:
        heartbeat_sweep()

    assert mock_delay.call_count == 2
    called_ids = {call.args[0] for call in mock_delay.call_args_list}
    assert called_ids == {player_a.id, player_b.id}


@pytest.mark.django_db
def test_heartbeat_sweep_noops_with_no_players() -> None:
    with patch(
        'anthias_server.fleet.tasks.poll_player_heartbeat.delay'
    ) as mock_delay:
        heartbeat_sweep()

    mock_delay.assert_not_called()
