"""Tests for the local-admin-facing pairing endpoints (plan §8):
``player/pairing/start``, ``player/pairing/status``,
``player/pairing/cancel``. The outbound-poll half of the handshake
(``fleet_link.tasks.poll_fleet_pairing``) has its own test module.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from anthias_server.fleet_link.models import FleetPairing

_FLEET_URL = 'https://fleet.example.com'


@pytest.mark.django_db
def test_pairing_status_is_standalone_by_default() -> None:
    response = Client().get(reverse('api:player_pairing_status_v2'))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        'status': 'standalone',
        'fleet_base_url': None,
        'pairing_user_code': None,
        'pairing_code_expires_at': None,
    }


@pytest.mark.django_db
def test_pairing_start_creates_pending_pairing() -> None:
    response = Client().post(
        reverse('api:player_pairing_start_v2'),
        {'fleet_base_url': _FLEET_URL},
        content_type='application/json',
    )

    assert response.status_code == 201
    body = response.json()
    assert body['status'] == 'pending'
    assert body['fleet_base_url'] == _FLEET_URL
    assert body['pairing_user_code']
    assert body['pairing_code_expires_at']

    pairing = FleetPairing.objects.get()
    assert pairing.status == FleetPairing.PENDING
    assert pairing.pairing_user_code == body['pairing_user_code']
    assert pairing.pairing_device_code
    # The raw device code is the Player's own secret proof-of-identity
    # for the poll handshake — never returned to the caller, even the
    # local admin who just started the pairing.
    assert 'pairing_device_code' not in body
    assert 'device_code' not in body


@pytest.mark.django_db
def test_pairing_start_rejects_when_already_pending() -> None:
    FleetPairing.objects.create(
        fleet_base_url=_FLEET_URL, status=FleetPairing.PENDING
    )

    response = Client().post(
        reverse('api:player_pairing_start_v2'),
        {'fleet_base_url': _FLEET_URL},
        content_type='application/json',
    )

    assert response.status_code == 409
    assert FleetPairing.objects.count() == 1


@pytest.mark.django_db
def test_pairing_start_rejects_when_already_active() -> None:
    FleetPairing.objects.create(
        fleet_base_url=_FLEET_URL, status=FleetPairing.ACTIVE
    )

    response = Client().post(
        reverse('api:player_pairing_start_v2'),
        {'fleet_base_url': _FLEET_URL},
        content_type='application/json',
    )

    assert response.status_code == 409


@pytest.mark.django_db
def test_pairing_start_rejects_invalid_url() -> None:
    response = Client().post(
        reverse('api:player_pairing_start_v2'),
        {'fleet_base_url': 'not-a-url'},
        content_type='application/json',
    )

    assert response.status_code == 400
    assert FleetPairing.objects.count() == 0


@pytest.mark.django_db
def test_pairing_status_reflects_pending_pairing() -> None:
    expires_at = timezone.now() + timedelta(minutes=10)
    FleetPairing.objects.create(
        fleet_base_url=_FLEET_URL,
        status=FleetPairing.PENDING,
        pairing_user_code='ABCD-1234',
        pairing_device_code='super-secret',
        pairing_code_expires_at=expires_at,
    )

    body = Client().get(reverse('api:player_pairing_status_v2')).json()

    assert body['status'] == 'pending'
    assert body['fleet_base_url'] == _FLEET_URL
    assert body['pairing_user_code'] == 'ABCD-1234'
    assert 'pairing_device_code' not in body


@pytest.mark.django_db
def test_pairing_cancel_deletes_pending_pairing() -> None:
    FleetPairing.objects.create(
        fleet_base_url=_FLEET_URL,
        status=FleetPairing.PENDING,
        pairing_user_code='ABCD-1234',
    )

    response = Client().post(
        reverse('api:player_pairing_cancel_v2'),
        {},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert response.json()['status'] == 'standalone'
    assert FleetPairing.objects.count() == 0


@pytest.mark.django_db
def test_pairing_cancel_404_when_nothing_pending() -> None:
    response = Client().post(
        reverse('api:player_pairing_cancel_v2'),
        {},
        content_type='application/json',
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_pairing_cancel_404_when_active() -> None:
    FleetPairing.objects.create(
        fleet_base_url=_FLEET_URL, status=FleetPairing.ACTIVE
    )

    response = Client().post(
        reverse('api:player_pairing_cancel_v2'),
        {},
        content_type='application/json',
    )

    assert response.status_code == 404
    assert FleetPairing.objects.filter(status=FleetPairing.ACTIVE).exists()
