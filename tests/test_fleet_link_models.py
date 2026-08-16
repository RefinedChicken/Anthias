"""Tests for the Phase-1 Player/Fleet-Server foundation models:
``Asset.origin``/``authority`` and the new ``fleet_link`` app
(``PlayerIdentity``, ``FleetPairing``, ``SyncState``, ``Command``).

No pairing/sync logic exists yet — these only verify the schema-level
invariants the later phases will build on.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError

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
    return Asset.objects.create(**defaults)  # type: ignore[arg-type]


@pytest.mark.django_db
def test_asset_defaults_to_local_origin_and_authority() -> None:
    asset = _make_asset()
    assert asset.origin == AUTHORITY_LOCAL
    assert asset.authority == AUTHORITY_LOCAL


@pytest.mark.django_db
def test_asset_origin_is_immutable_after_creation() -> None:
    asset = _make_asset(origin=AUTHORITY_FLEET)
    asset.origin = AUTHORITY_LOCAL

    with pytest.raises(ValueError, match='immutable'):
        asset.save()


@pytest.mark.django_db
def test_asset_origin_unchanged_save_is_a_no_op() -> None:
    asset = _make_asset(origin=AUTHORITY_FLEET)
    asset.name = 'renamed'
    asset.save()  # must not raise: origin field value didn't change

    asset.refresh_from_db()
    assert asset.name == 'renamed'
    assert asset.origin == AUTHORITY_FLEET


@pytest.mark.django_db
def test_asset_authority_is_freely_mutable() -> None:
    """Unlike origin, authority flips as sync/unpair dictate."""
    asset = _make_asset(origin=AUTHORITY_FLEET, authority=AUTHORITY_FLEET)

    asset.authority = AUTHORITY_LOCAL
    asset.save()

    asset.refresh_from_db()
    assert asset.authority == AUTHORITY_LOCAL
    assert asset.origin == AUTHORITY_FLEET  # untouched


@pytest.mark.django_db
def test_player_identity_get_or_create_is_a_stable_singleton() -> None:
    first = PlayerIdentity.get_or_create()
    second = PlayerIdentity.get_or_create()

    assert first.pk == second.pk
    assert first.device_id == second.device_id
    assert PlayerIdentity.objects.count() == 1


@pytest.mark.django_db
def test_fleet_pairing_current_is_none_when_standalone() -> None:
    assert FleetPairing.current() is None


@pytest.mark.django_db
def test_fleet_pairing_current_ignores_revoked_rows() -> None:
    FleetPairing.objects.create(
        fleet_base_url='https://old-fleet.example.com',
        status=FleetPairing.REVOKED,
    )
    active = FleetPairing.objects.create(
        fleet_base_url='https://fleet.example.com',
        status=FleetPairing.ACTIVE,
    )

    assert FleetPairing.current() == active


@pytest.mark.django_db
def test_fleet_pairing_default_authority_is_local_on_every_domain() -> None:
    pairing = FleetPairing.objects.create(
        fleet_base_url='https://fleet.example.com'
    )

    assert pairing.content_authority == AUTHORITY_LOCAL
    assert pairing.playlist_authority == AUTHORITY_LOCAL
    assert pairing.schedule_authority == AUTHORITY_LOCAL
    assert pairing.config_authority == AUTHORITY_LOCAL


@pytest.mark.django_db
def test_sync_state_domain_is_unique() -> None:
    SyncState.objects.create(domain=SyncState.MEDIA)

    with pytest.raises(IntegrityError):
        SyncState.objects.create(domain=SyncState.MEDIA)


@pytest.mark.django_db
def test_command_id_is_unique_for_idempotent_redelivery() -> None:
    Command.objects.create(command_id='cmd-1', type=Command.REBOOT)

    with pytest.raises(IntegrityError):
        Command.objects.create(command_id='cmd-1', type=Command.SHUTDOWN)
