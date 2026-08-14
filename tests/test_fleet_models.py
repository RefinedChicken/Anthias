"""Tests for the fleet app's Player/PlayerGroup models and the Fernet
encryption wrapper their api_token field relies on."""

from __future__ import annotations

import pytest

from anthias_server.fleet.crypto import decrypt_api_token, encrypt_api_token
from anthias_server.fleet.models import Player, PlayerGroup

_RAW_TOKEN = 'ant_fixture-raw-token-value'  # NOSONAR


def test_encrypt_api_token_round_trips() -> None:
    encrypted = encrypt_api_token(_RAW_TOKEN)
    assert encrypted != _RAW_TOKEN
    assert decrypt_api_token(encrypted) == _RAW_TOKEN


def test_encrypt_api_token_is_not_deterministic() -> None:
    # Fernet includes a random IV + timestamp per encryption, so two
    # ciphertexts of the same plaintext must differ — a fixed digest
    # here would mean tokens with the same value are distinguishable
    # by ciphertext alone in the DB.
    first = encrypt_api_token(_RAW_TOKEN)
    second = encrypt_api_token(_RAW_TOKEN)
    assert first != second
    assert decrypt_api_token(first) == decrypt_api_token(second) == _RAW_TOKEN


@pytest.mark.django_db
def test_player_set_and_get_api_token_round_trips() -> None:
    player = Player(name='Lobby TV', base_url='http://192.168.1.50:8080')
    player.set_api_token(_RAW_TOKEN)
    player.save()

    # api_token_encrypted is what's actually persisted — assert the
    # plaintext never lands in the DB column.
    assert _RAW_TOKEN not in player.api_token_encrypted

    player.refresh_from_db()
    assert player.get_api_token() == _RAW_TOKEN


@pytest.mark.django_db
def test_player_group_set_null_on_group_delete() -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    player = Player(
        name='Lobby TV', base_url='http://192.168.1.50:8080', group=group
    )
    player.set_api_token(_RAW_TOKEN)
    player.save()

    group.delete()
    player.refresh_from_db()
    assert player.group_id is None


@pytest.mark.django_db
def test_player_group_str_and_default_ordering() -> None:
    PlayerGroup.objects.create(name='Warehouse')
    PlayerGroup.objects.create(name='Lobby')

    names = list(PlayerGroup.objects.values_list('name', flat=True))
    assert names == ['Lobby', 'Warehouse']
    assert str(PlayerGroup.objects.get(name='Lobby')) == 'Lobby'


def _make_player(**overrides: object) -> Player:
    player = Player(
        name='Lobby TV', base_url='http://192.168.1.50:8080', **overrides
    )
    player.set_api_token(_RAW_TOKEN)
    return player


def test_health_flags_empty_for_a_freshly_polled_healthy_player() -> None:
    player = _make_player(
        up_to_date=True, loadavg_15min=0.1, memory={'low_ram': False}
    )
    assert player.health_flags == []


def test_health_flags_flags_update_available() -> None:
    player = _make_player(up_to_date=False)
    labels = [f['label'] for f in player.health_flags]
    assert 'Update available' in labels


def test_health_flags_warns_on_elevated_load() -> None:
    player = _make_player(loadavg_15min=Player.LOAD_AVG_WARN)
    assert player.health_flags == [
        {'level': 'warning', 'label': 'Elevated load average'}
    ]


def test_health_flags_marks_critical_on_high_load() -> None:
    player = _make_player(loadavg_15min=Player.LOAD_AVG_CRIT)
    assert player.health_flags == [
        {'level': 'critical', 'label': 'High load average'}
    ]


def test_health_flags_flags_low_ram_as_informational() -> None:
    player = _make_player(memory={'low_ram': True})
    assert player.health_flags == [
        {'level': 'info', 'label': 'Low-RAM device'}
    ]


def test_health_flags_flags_display_off() -> None:
    player = _make_player(display_power='off')
    assert player.health_flags == [{'level': 'info', 'label': 'Display off'}]


def test_health_flags_never_polled_player_has_no_flags() -> None:
    # A brand-new row (loadavg_15min/up_to_date both None, memory={})
    # must not spuriously flag anything from absent data.
    player = _make_player()
    assert player.health_flags == []
