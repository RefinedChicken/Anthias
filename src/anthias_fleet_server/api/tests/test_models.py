"""Tests for Fleet Server model-level invariants: Playlist versioning
and the Deployment/Media uniqueness constraints."""

from __future__ import annotations

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError

from anthias_fleet_server.core.models import (
    Deployment,
    Media,
    Organization,
    Playlist,
)


def _make_org() -> Organization:
    return Organization.objects.create(name='Test Org')


def _make_media(org: Organization, checksum: str = 'a' * 64) -> Media:
    return Media.objects.create(
        organization=org,
        file=SimpleUploadedFile('a.jpg', b'x'),
        filename='a.jpg',
        size=1,
        checksum=checksum,
    )


@pytest.mark.django_db
def test_playlist_bump_version_increments_and_reloads() -> None:
    org = _make_org()
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    assert playlist.version == 1

    playlist.bump_version()

    assert playlist.version == 2
    playlist.refresh_from_db()
    assert playlist.version == 2


@pytest.mark.django_db
def test_media_checksum_is_unique_per_organization() -> None:
    org = _make_org()
    _make_media(org, checksum='b' * 64)

    with pytest.raises(IntegrityError):
        _make_media(org, checksum='b' * 64)


@pytest.mark.django_db
def test_media_checksum_may_repeat_across_organizations() -> None:
    org_a = _make_org()
    org_b = Organization.objects.create(name='Other Org')

    _make_media(org_a, checksum='c' * 64)
    # Must not raise: same bytes, different organization's library.
    _make_media(org_b, checksum='c' * 64)


@pytest.mark.django_db
def test_deployment_requires_exactly_one_target() -> None:
    org = _make_org()
    media = _make_media(org)
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    playlist.items.create(media=media, order=0)

    with pytest.raises(IntegrityError):
        Deployment.objects.create(
            organization=org, playlist=playlist, version=1
        )


@pytest.mark.django_db
def test_deployment_rejects_both_player_and_group() -> None:
    from anthias_fleet_server.core.models import Group, Player

    org = _make_org()
    playlist = Playlist.objects.create(organization=org, name='Lobby')
    player = Player.objects.create(
        organization=org, name='Lobby TV', device_id=uuid.uuid4()
    )
    group = Group.objects.create(organization=org, name='Lobby')

    with pytest.raises(IntegrityError):
        Deployment.objects.create(
            organization=org,
            playlist=playlist,
            player=player,
            group=group,
            version=1,
        )
