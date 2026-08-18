"""Tests for the pairing protocol (plan §8): the unauthenticated
poll/ack endpoints in ``api.pairing_views``, plus the admin-facing
list/approve/revoke surface on ``PairingRequestViewSet``/``PlayerViewSet``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

if TYPE_CHECKING:
    # Only exists in django-stubs, not at runtime — see the
    # ``from __future__ import annotations`` above, which is what
    # keeps this import out of the runtime path entirely.
    from django.test.client import _MonkeyPatchedWSGIResponse

from anthias_fleet_server.api.authentication import hash_player_credential
from anthias_fleet_server.core.models import (
    Membership,
    Organization,
    PairingRequest,
    Player,
    PlayerCredential,
)

_PWD = 'fixture-pairing-pwd'  # NOSONAR


def _make_org(name: str = 'Test Org') -> Organization:
    return Organization.objects.create(name=name)


def _make_member(
    org: Organization, role: str, username: str = 'alice'
) -> User:
    user = User.objects.create_user(username=username, password=_PWD)
    Membership.objects.create(user=user, organization=org, role=role)
    return user


def _client_as(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _poll(
    device_code: str,
    user_code: str,
    device_id: uuid.UUID | None = None,
    device_label: str = '',
) -> _MonkeyPatchedWSGIResponse:
    # Cleared per call rather than relying on the once-per-test autouse
    # fixture: most tests here make several sequential polls and don't
    # want the per-device-code rate gate (api.pairing_views.
    # _device_code_poll_allowed) tripping between them — that gate gets
    # its own dedicated test below instead.
    cache.clear()
    body = {'device_code': device_code, 'user_code': user_code}
    if device_id is not None:
        body['device_id'] = str(device_id)
    if device_label:
        body['device_label'] = device_label
    return Client().post(
        '/api/pairing/poll', body, content_type='application/json'
    )


def _approve_pairing(
    admin_client: Client, pairing_request: PairingRequest
) -> _MonkeyPatchedWSGIResponse:
    return admin_client.post(
        f'/api/pairing-requests/{pairing_request.id}/approve/',
        {},
        content_type='application/json',
    )


# ---------------------------------------------------------------------------
# Poll: registration, idempotency, collisions, expiry


@pytest.mark.django_db
def test_first_poll_creates_pending_request() -> None:
    device_id = uuid.uuid4()
    response = _poll('devicecode1', 'ABCD-1234', device_id, 'Lobby TV')

    assert response.status_code == 200
    assert response.json() == {'status': 'pending'}
    pairing_request = PairingRequest.objects.get()
    assert pairing_request.status == PairingRequest.PENDING
    assert pairing_request.device_id == device_id
    assert pairing_request.label == 'Lobby TV'
    assert pairing_request.device_code_hash == hash_player_credential(
        'devicecode1'
    )
    assert pairing_request.organization is None


@pytest.mark.django_db
def test_repeat_poll_returns_pending_without_duplicating() -> None:
    _poll('devicecode1', 'ABCD-1234')
    response = _poll('devicecode1', 'ABCD-1234')

    assert response.status_code == 200
    assert response.json() == {'status': 'pending'}
    assert PairingRequest.objects.count() == 1


@pytest.mark.django_db
def test_user_code_collision_from_different_device_is_rejected() -> None:
    _poll('devicecode1', 'ABCD-1234')
    response = _poll('devicecode2', 'ABCD-1234')

    assert response.status_code == 409
    assert response.json() == {'status': 'code_collision'}
    assert PairingRequest.objects.count() == 1


@pytest.mark.django_db
def test_expired_pending_request_is_reported_and_deleted() -> None:
    _poll('devicecode1', 'ABCD-1234')
    PairingRequest.objects.update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    response = _poll('devicecode1', 'ABCD-1234')

    assert response.status_code == 200
    assert response.json() == {'status': 'expired'}
    assert PairingRequest.objects.count() == 0


@pytest.mark.django_db
def test_device_code_poll_is_rate_limited() -> None:
    cache.clear()
    client = Client()
    body = {'device_code': 'ratelimited', 'user_code': 'AAAA-1111'}

    first = client.post(
        '/api/pairing/poll', body, content_type='application/json'
    )
    second = client.post(
        '/api/pairing/poll', body, content_type='application/json'
    )

    assert first.status_code == 200
    assert second.status_code == 429


# ---------------------------------------------------------------------------
# Approve (admin-facing)


@pytest.mark.django_db
def test_approve_creates_player_and_binds_device_id() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    device_id = uuid.uuid4()
    _poll('devicecode1', 'ABCD-1234', device_id, 'Lobby TV')
    pairing_request = PairingRequest.objects.get()

    response = _approve_pairing(admin_client, pairing_request)

    assert response.status_code == 200
    pairing_request.refresh_from_db()
    assert pairing_request.status == PairingRequest.APPROVED
    assert pairing_request.organization == org
    assert pairing_request.approved_by == operator
    assert pairing_request.player is not None
    assert pairing_request.player.device_id == device_id
    assert pairing_request.player.name == 'Lobby TV'


@pytest.mark.django_db
def test_approve_reuses_existing_player_with_matching_device_id() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    device_id = uuid.uuid4()
    existing = Player.objects.create(
        organization=org, name='Lobby', device_id=device_id
    )
    _poll('devicecode1', 'ABCD-1234', device_id)
    pairing_request = PairingRequest.objects.get()

    _approve_pairing(admin_client, pairing_request)

    pairing_request.refresh_from_db()
    assert pairing_request.player_id == existing.id
    assert Player.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_approve_with_player_id_bound_to_different_device_is_rejected() -> (
    None
):
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    other_player = Player.objects.create(
        organization=org, name='Existing', device_id=uuid.uuid4()
    )
    _poll('devicecode1', 'ABCD-1234', uuid.uuid4())
    pairing_request = PairingRequest.objects.get()

    response = admin_client.post(
        f'/api/pairing-requests/{pairing_request.id}/approve/',
        {'player_id': other_player.id},
        content_type='application/json',
    )

    assert response.status_code == 403
    pairing_request.refresh_from_db()
    assert pairing_request.status == PairingRequest.PENDING


@pytest.mark.django_db
def test_approve_requires_pending_status() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    _poll('devicecode1', 'ABCD-1234')
    pairing_request = PairingRequest.objects.get()
    _approve_pairing(admin_client, pairing_request)

    response = _approve_pairing(admin_client, pairing_request)

    assert response.status_code == 403


@pytest.mark.django_db
def test_viewer_can_list_but_not_approve_pending_requests() -> None:
    org = _make_org()
    viewer = _make_member(org, Membership.VIEWER)
    client = _client_as(viewer)
    _poll('devicecode1', 'ABCD-1234')
    pairing_request = PairingRequest.objects.get()

    list_response = client.get('/api/pairing-requests/')
    approve_response = _approve_pairing(client, pairing_request)

    assert list_response.status_code == 200
    assert approve_response.status_code == 403


# ---------------------------------------------------------------------------
# Credential delivery: reissue-until-ack, ack, revoke


@pytest.mark.django_db
def test_poll_after_approval_delivers_credential_once_per_call() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    device_id = uuid.uuid4()
    _poll('devicecode1', 'ABCD-1234', device_id)
    pairing_request = PairingRequest.objects.get()
    _approve_pairing(admin_client, pairing_request)

    response = _poll('devicecode1', 'ABCD-1234', device_id)

    body = response.json()
    assert body['status'] == 'approved'
    pairing_request.refresh_from_db()
    assert body['player_id'] == str(pairing_request.player_id)
    credential = PlayerCredential.objects.get(player=pairing_request.player)
    assert credential.token_hash == hash_player_credential(body['credential'])
    assert credential.revoked_at is None


@pytest.mark.django_db
def test_repeat_poll_after_approval_reissues_and_revokes_previous() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    _poll('devicecode1', 'ABCD-1234', uuid.uuid4())
    pairing_request = PairingRequest.objects.get()
    _approve_pairing(admin_client, pairing_request)

    first = _poll('devicecode1', 'ABCD-1234').json()
    second = _poll('devicecode1', 'ABCD-1234').json()

    assert first['credential'] != second['credential']
    old_credential = PlayerCredential.objects.get(
        token_hash=hash_player_credential(first['credential'])
    )
    new_credential = PlayerCredential.objects.get(
        token_hash=hash_player_credential(second['credential'])
    )
    assert old_credential.revoked_at is not None
    assert new_credential.revoked_at is None
    pairing_request.refresh_from_db()
    assert (
        PlayerCredential.objects.filter(
            player=pairing_request.player, revoked_at__isnull=True
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_ack_completes_pairing_and_stops_further_issuance() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    _poll('devicecode1', 'ABCD-1234', uuid.uuid4())
    pairing_request = PairingRequest.objects.get()
    _approve_pairing(admin_client, pairing_request)
    raw_credential = _poll('devicecode1', 'ABCD-1234').json()['credential']

    ack_response = Client().post(
        '/api/pairing/ack',
        {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {raw_credential}',
    )

    assert ack_response.status_code == 200
    pairing_request.refresh_from_db()
    assert pairing_request.status == PairingRequest.COMPLETED

    poll_after_ack = _poll('devicecode1', 'ABCD-1234').json()
    assert poll_after_ack == {'status': 'completed'}
    assert (
        PlayerCredential.objects.filter(
            player=pairing_request.player, revoked_at__isnull=True
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_ack_with_invalid_credential_is_rejected() -> None:
    response = Client().post(
        '/api/pairing/ack',
        {},
        content_type='application/json',
        HTTP_AUTHORIZATION='Bearer not-a-real-credential',
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_ack_with_no_approved_request_for_player_is_not_found() -> None:
    org = _make_org()
    player = Player.objects.create(organization=org, name='Orphan')
    from anthias_fleet_server.api.authentication import (
        issue_player_credential,
    )

    _credential_row, raw_credential = issue_player_credential(player)

    response = Client().post(
        '/api/pairing/ack',
        {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {raw_credential}',
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_operator_can_revoke_player_credential() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    admin_client = _client_as(operator)
    _poll('devicecode1', 'ABCD-1234', uuid.uuid4())
    pairing_request = PairingRequest.objects.get()
    _approve_pairing(admin_client, pairing_request)
    raw_credential = _poll('devicecode1', 'ABCD-1234').json()['credential']
    pairing_request.refresh_from_db()
    player = pairing_request.player
    assert player is not None

    response = admin_client.post(
        f'/api/players/{player.id}/revoke-credential/',
        {},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert response.json() == {'revoked': 1}
    credential = PlayerCredential.objects.get(player=player)
    assert credential.revoked_at is not None

    ack_response = Client().post(
        '/api/pairing/ack',
        {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {raw_credential}',
    )
    assert ack_response.status_code == 401
