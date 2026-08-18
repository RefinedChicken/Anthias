"""Tests for ``fleet_link.tasks.poll_fleet_pairing`` — the Celery-beat
pairing poll (plan §8 steps 3-6). Every outbound call goes through
``requests``, which is mocked explicitly in every test here: nothing
in the root ``conftest.py`` stops a real network call, unlike Redis
(mocked globally) or the DB (sandboxed by ``pytest.mark.django_db``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.utils import timezone

from anthias_server.fleet_link.models import FleetPairing
from anthias_server.fleet_link.tasks import poll_fleet_pairing

_FLEET_URL = 'https://fleet.example.com'


def _mock_response(
    status_code: int = 200, json_data: dict[str, Any] | None = None
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    if json_data is None:
        response.json.side_effect = ValueError('no body')
    else:
        response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


def _make_pending(**overrides: Any) -> FleetPairing:
    defaults: dict[str, Any] = {
        'fleet_base_url': _FLEET_URL,
        'status': FleetPairing.PENDING,
        'pairing_user_code': 'ABCD-1234',
        'pairing_device_code': 'device-secret',
        'pairing_code_expires_at': timezone.now() + timedelta(minutes=10),
    }
    defaults.update(overrides)
    return FleetPairing.objects.create(**defaults)


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_noop_when_no_pairing(mock_post: MagicMock) -> None:
    poll_fleet_pairing()

    mock_post.assert_not_called()


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_noop_when_pairing_active(mock_post: MagicMock) -> None:
    FleetPairing.objects.create(
        fleet_base_url=_FLEET_URL, status=FleetPairing.ACTIVE
    )

    poll_fleet_pairing()

    mock_post.assert_not_called()


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_expired_pending_pairing_is_deleted_without_polling(
    mock_post: MagicMock,
) -> None:
    _make_pending(
        pairing_code_expires_at=timezone.now() - timedelta(seconds=1)
    )

    poll_fleet_pairing()

    mock_post.assert_not_called()
    assert FleetPairing.objects.count() == 0


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_network_error_leaves_pairing_pending(mock_post: MagicMock) -> None:
    pairing = _make_pending()
    mock_post.side_effect = requests.exceptions.ConnectionError('unreachable')

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_pending_response_leaves_pairing_untouched(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    mock_post.return_value = _mock_response(json_data={'status': 'pending'})

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING
    assert pairing.pairing_device_code == 'device-secret'


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_code_collision_deletes_pairing(mock_post: MagicMock) -> None:
    _make_pending()
    mock_post.return_value = _mock_response(
        status_code=409, json_data={'status': 'code_collision'}
    )

    poll_fleet_pairing()

    assert FleetPairing.objects.count() == 0


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_expired_status_response_deletes_pairing(
    mock_post: MagicMock,
) -> None:
    _make_pending()
    mock_post.return_value = _mock_response(json_data={'status': 'expired'})

    poll_fleet_pairing()

    assert FleetPairing.objects.count() == 0


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_rate_limited_response_leaves_pairing_pending(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    mock_post.return_value = _mock_response(status_code=429)

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_http_error_response_leaves_pairing_pending(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    mock_post.return_value = _mock_response(status_code=500)

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_non_json_response_leaves_pairing_pending(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    mock_post.return_value = _mock_response(json_data=None)

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_unexpected_status_leaves_pairing_pending(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    mock_post.return_value = _mock_response(json_data={'status': 'bogus'})

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_approved_response_completes_pairing(mock_post: MagicMock) -> None:
    pairing = _make_pending()
    poll_response = _mock_response(
        json_data={
            'status': 'approved',
            'credential': 'fpc_rawtoken',
            'player_id': '42',
        }
    )
    ack_response = _mock_response(json_data={'status': 'completed'})
    mock_post.side_effect = [poll_response, ack_response]

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.ACTIVE
    assert pairing.device_credential == 'fpc_rawtoken'
    assert pairing.paired_at is not None
    assert pairing.pairing_user_code is None
    assert pairing.pairing_device_code is None
    assert pairing.pairing_code_expires_at is None

    poll_call, ack_call = mock_post.call_args_list
    assert poll_call.args[0] == f'{_FLEET_URL}/api/pairing/poll'
    assert ack_call.args[0] == f'{_FLEET_URL}/api/pairing/ack'
    assert ack_call.kwargs['headers']['Authorization'] == 'Bearer fpc_rawtoken'


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_approved_response_without_credential_leaves_pairing_pending(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    mock_post.return_value = _mock_response(
        json_data={'status': 'approved', 'player_id': '42'}
    )

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING
    assert pairing.device_credential is None
    mock_post.assert_called_once()


@pytest.mark.django_db
@patch('anthias_server.fleet_link.tasks.requests.post')
def test_approved_response_but_ack_failure_leaves_pairing_pending(
    mock_post: MagicMock,
) -> None:
    pairing = _make_pending()
    poll_response = _mock_response(
        json_data={'status': 'approved', 'credential': 'fpc_rawtoken'}
    )
    mock_post.side_effect = [
        poll_response,
        requests.exceptions.ConnectionError('ack unreachable'),
    ]

    poll_fleet_pairing()

    pairing.refresh_from_db()
    assert pairing.status == FleetPairing.PENDING
    assert pairing.device_credential is None
    # The code pair is still intact — the next tick repeats the poll
    # from scratch against a freshly (re)issued credential, per the
    # Fleet-side "reissue until ack" contract.
    assert pairing.pairing_device_code == 'device-secret'
