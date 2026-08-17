"""Tests for the Fleet API viewsets: RBAC, organization scoping, and
the Media upload-dedup behavior.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from anthias_fleet_server.core.models import (
    Deployment,
    Group,
    Media,
    Membership,
    Organization,
    Player,
    Playlist,
)

_PWD = 'fixture-fleet-api-pwd'  # NOSONAR


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


# ---------------------------------------------------------------------------
# Auth / RBAC


@pytest.mark.django_db
def test_anonymous_request_is_rejected() -> None:
    response = Client().get('/api/players/')
    assert response.status_code in (401, 403)


@pytest.mark.django_db
def test_user_without_membership_is_rejected() -> None:
    user = User.objects.create_user(username='nobody', password=_PWD)
    response = _client_as(user).get('/api/players/')
    assert response.status_code == 403


@pytest.mark.django_db
def test_viewer_can_read_but_not_write_groups() -> None:
    org = _make_org()
    viewer = _make_member(org, Membership.VIEWER)
    client = _client_as(viewer)

    assert client.get('/api/groups/').status_code == 200

    response = client.post(
        '/api/groups/', {'name': 'Lobby'}, content_type='application/json'
    )
    assert response.status_code == 403
    assert not Group.objects.filter(name='Lobby').exists()


@pytest.mark.django_db
def test_operator_can_write_groups() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    client = _client_as(operator)

    response = client.post(
        '/api/groups/', {'name': 'Lobby'}, content_type='application/json'
    )

    assert response.status_code == 201
    assert Group.objects.filter(organization=org, name='Lobby').exists()


@pytest.mark.django_db
def test_operator_cannot_change_membership_roles() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR, username='op')
    other = _make_member(org, Membership.VIEWER, username='vw')
    membership = Membership.objects.get(user=other)

    response = _client_as(operator).patch(
        f'/api/memberships/{membership.pk}/',
        {'role': Membership.ADMINISTRATOR},
        content_type='application/json',
    )

    assert response.status_code == 403
    membership.refresh_from_db()
    assert membership.role == Membership.VIEWER


@pytest.mark.django_db
def test_administrator_can_change_membership_roles() -> None:
    org = _make_org()
    admin = _make_member(org, Membership.ADMINISTRATOR, username='admin')
    other = _make_member(org, Membership.VIEWER, username='vw')
    membership = Membership.objects.get(user=other)

    response = _client_as(admin).patch(
        f'/api/memberships/{membership.pk}/',
        {'role': Membership.OPERATOR},
        content_type='application/json',
    )

    assert response.status_code == 200
    membership.refresh_from_db()
    assert membership.role == Membership.OPERATOR


# ---------------------------------------------------------------------------
# Organization scoping


@pytest.mark.django_db
def test_players_are_scoped_to_the_callers_organization() -> None:
    org_a = _make_org('Org A')
    org_b = _make_org('Org B')
    Player.objects.create(organization=org_a, name='Org A Player')
    Player.objects.create(organization=org_b, name='Org B Player')

    operator_a = _make_member(org_a, Membership.OPERATOR, username='a-op')

    response = _client_as(operator_a).get('/api/players/')

    names = {row['name'] for row in response.json()['results']}
    assert names == {'Org A Player'}


@pytest.mark.django_db
def test_created_player_is_attached_to_callers_organization() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)

    response = _client_as(operator).post(
        '/api/players/',
        {'name': 'Lobby TV'},
        content_type='application/json',
    )

    assert response.status_code == 201
    player = Player.objects.get(name='Lobby TV')
    assert player.organization_id == org.id


@pytest.mark.django_db
def test_player_cannot_reference_a_group_from_another_organization() -> None:
    org_a = _make_org('Org A')
    org_b = _make_org('Org B')
    foreign_group = Group.objects.create(organization=org_b, name='Chapel')
    operator = _make_member(org_a, Membership.OPERATOR)

    response = _client_as(operator).post(
        '/api/players/',
        {'name': 'Lobby TV', 'groups': [foreign_group.pk]},
        content_type='application/json',
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Media upload / dedup


@pytest.mark.django_db
def test_media_upload_computes_checksum_and_size() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)

    response = _client_as(operator).post(
        '/api/media/',
        {'file': SimpleUploadedFile('logo.png', b'hello world')},
    )

    assert response.status_code == 201
    media = Media.objects.get(organization=org)
    assert media.size == len(b'hello world')
    assert media.checksum  # sha256 hex digest, non-empty
    assert media.filename == 'logo.png'


@pytest.mark.django_db
def test_media_reupload_of_identical_bytes_returns_existing_row() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    client = _client_as(operator)

    first = client.post(
        '/api/media/', {'file': SimpleUploadedFile('logo.png', b'same')}
    )
    second = client.post(
        '/api/media/',
        {'file': SimpleUploadedFile('logo-renamed.png', b'same')},
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()['id'] == second.json()['id']
    assert Media.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_media_upload_with_different_bytes_creates_separate_rows() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    client = _client_as(operator)

    client.post('/api/media/', {'file': SimpleUploadedFile('a.png', b'aaa')})
    client.post('/api/media/', {'file': SimpleUploadedFile('b.png', b'bbb')})

    assert Media.objects.filter(organization=org).count() == 2


# ---------------------------------------------------------------------------
# Playlist items bump the playlist version; Deployments snapshot it


@pytest.mark.django_db
def test_adding_a_playlist_item_bumps_playlist_version() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    media = Media.objects.create(
        organization=org,
        file=SimpleUploadedFile('a.png', b'a'),
        filename='a.png',
        size=1,
        checksum='d' * 64,
    )

    response = _client_as(operator).post(
        '/api/playlist-items/',
        {'playlist': playlist.pk, 'media': media.id, 'order': 0},
        content_type='application/json',
    )

    assert response.status_code == 201
    playlist.refresh_from_db()
    assert playlist.version == 2


@pytest.mark.django_db
def test_removing_a_playlist_item_bumps_playlist_version() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    media = Media.objects.create(
        organization=org,
        file=SimpleUploadedFile('a.png', b'a'),
        filename='a.png',
        size=1,
        checksum='e' * 64,
    )
    item = playlist.items.create(media=media, order=0)
    playlist.refresh_from_db()
    version_after_create = playlist.version

    response = _client_as(operator).delete(f'/api/playlist-items/{item.pk}/')

    assert response.status_code == 204
    playlist.refresh_from_db()
    assert playlist.version == version_after_create + 1


@pytest.mark.django_db
def test_deployment_snapshots_playlist_version_at_creation() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    playlist.version = 5
    playlist.save(update_fields=['version'])
    player = Player.objects.create(
        organization=org, name='Lobby TV', device_id=uuid.uuid4()
    )

    response = _client_as(operator).post(
        '/api/deployments/',
        {'playlist': playlist.pk, 'player': player.pk},
        content_type='application/json',
    )

    assert response.status_code == 201
    deployment = Deployment.objects.get(playlist=playlist)
    assert deployment.version == 5
    assert deployment.created_by_id == operator.id


@pytest.mark.django_db
def test_deployment_rejects_both_player_and_group_via_api() -> None:
    org = _make_org()
    operator = _make_member(org, Membership.OPERATOR)
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    player = Player.objects.create(organization=org, name='Lobby TV')
    group = Group.objects.create(organization=org, name='Lobby Group')

    response = _client_as(operator).post(
        '/api/deployments/',
        {'playlist': playlist.pk, 'player': player.pk, 'group': group.pk},
        content_type='application/json',
    )

    assert response.status_code == 400
