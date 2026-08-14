"""Tests for Phase 1.5c — fleet pairing + settings lock.

Covers: the AnthiasAPIToken.purpose field and the
is_fleet_managed()/fleet_management_token() helpers (lib/auth.py), the
player-side pairing views (fleet_pairing_create/revoke), settings_save's
field-skipping while fleet-managed, DeviceSettingsViewV2.patch's
request.auth enforcement, PlayerAPIClient's new device-settings
methods, and the fleet console's player_settings view.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from anthias_common.http import AnthiasSession
from anthias_server.api.models import AnthiasAPIToken
from anthias_server.fleet.models import Player
from anthias_server.fleet.player_client import PlayerAPIClient
from anthias_server.lib.auth import (
    fleet_management_token,
    is_fleet_managed,
    issue_api_token,
)

_PWD_ADMIN = 'fixture-fleet-pairing-pwd'  # NOSONAR
_RAW_TOKEN = 'ant_fixture-fleet-pairing-token'  # NOSONAR
_FLEET_URLCONF = 'anthias_server.django_project.fleet_urls'


@pytest.fixture
def client() -> Client:
    return Client()


def _make_admin(username: str = 'alice') -> User:
    return User.objects.create_superuser(
        username=username, password=_PWD_ADMIN
    )


# ---------------------------------------------------------------------------
# is_fleet_managed / fleet_management_token / issue_api_token(purpose=...)


@pytest.mark.django_db
def test_is_fleet_managed_false_with_no_tokens() -> None:
    assert is_fleet_managed() is False
    assert fleet_management_token() is None


@pytest.mark.django_db
def test_is_fleet_managed_false_with_only_general_tokens() -> None:
    admin = _make_admin()
    issue_api_token(admin, 'some integration')
    assert is_fleet_managed() is False


@pytest.mark.django_db
def test_issue_api_token_with_fleet_management_purpose() -> None:
    admin = _make_admin()
    token_row, _ = issue_api_token(
        admin,
        'Fleet pairing',
        purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT,
    )
    assert token_row.purpose == AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    assert is_fleet_managed() is True
    assert fleet_management_token() == token_row


@pytest.mark.django_db
def test_default_purpose_is_general() -> None:
    admin = _make_admin()
    token_row, _ = issue_api_token(admin, 'plain token')
    assert token_row.purpose == AnthiasAPIToken.PURPOSE_GENERAL


# ---------------------------------------------------------------------------
# fleet_pairing_create / fleet_pairing_revoke


@pytest.mark.django_db
def test_fleet_pairing_create_issues_fleet_scoped_token() -> None:
    admin = _make_admin()
    client = Client()
    client.force_login(admin)

    response = client.post(reverse('anthias_app:fleet_pairing_create'))

    assert response.status_code == 302
    token = AnthiasAPIToken.objects.get(
        purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    )
    assert token.user == admin
    assert client.session['new_fleet_pairing_token'].startswith('ant_')


@pytest.mark.django_db
def test_new_pairing_token_renders_once_even_though_already_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: creating a pairing token makes is_fleet_managed()
    true immediately, so the very next render of the settings page (the
    one meant to show the one-time reveal) already takes the "Paired"
    branch — the reveal must not be nested inside the "not yet paired"
    branch, or it becomes unreachable. Caught by hand while verifying
    against a live server; this is the regression test that should have
    caught it here first."""
    monkeypatch.setattr(
        'anthias_server.app.page_context.device_helper.parse_cpu_info',
        lambda: {'cpu_count': 0},
    )

    admin = _make_admin()
    client = Client()
    client.force_login(admin)
    client.post(reverse('anthias_app:fleet_pairing_create'))
    raw_token = client.session['new_fleet_pairing_token']

    assert is_fleet_managed() is True

    first = client.get(reverse('anthias_app:settings'))
    assert first.status_code == 200
    assert raw_token in first.content.decode()
    assert 'new_fleet_pairing_token' not in client.session

    second = client.get(reverse('anthias_app:settings'))
    assert second.status_code == 200
    assert raw_token not in second.content.decode()


@pytest.mark.django_db
def test_fleet_pairing_create_replaces_existing_pairing_token() -> None:
    admin = _make_admin()
    first, _ = issue_api_token(
        admin, 'old pairing', purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    )
    client = Client()
    client.force_login(admin)

    client.post(reverse('anthias_app:fleet_pairing_create'))

    assert not AnthiasAPIToken.objects.filter(pk=first.pk).exists()
    assert (
        AnthiasAPIToken.objects.filter(
            purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_fleet_pairing_revoke_deletes_token_and_unlocks() -> None:
    admin = _make_admin()
    issue_api_token(
        admin, 'pairing', purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    )
    client = Client()
    client.force_login(admin)
    assert is_fleet_managed() is True

    response = client.post(reverse('anthias_app:fleet_pairing_revoke'))

    assert response.status_code == 302
    assert is_fleet_managed() is False


@pytest.mark.django_db
def test_fleet_pairing_revoke_when_not_paired_is_a_no_op() -> None:
    admin = _make_admin()
    client = Client()
    client.force_login(admin)

    response = client.post(reverse('anthias_app:fleet_pairing_revoke'))

    assert response.status_code == 302
    assert AnthiasAPIToken.objects.count() == 0


# ---------------------------------------------------------------------------
# settings_save skips locked fields while fleet-managed


@pytest.mark.django_db
def test_settings_save_skips_locked_fields_when_fleet_managed(
    tmp_path: Any,
) -> None:
    from anthias_server.settings import settings as device_settings

    original_conf_file = device_settings.conf_file
    try:
        device_settings.conf_file = str(tmp_path / 'anthias.conf')
        device_settings.save()
        device_settings['player_name'] = 'Original Name'
        device_settings.save()

        admin = _make_admin()
        issue_api_token(
            admin,
            'pairing',
            purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT,
        )
        client = Client()
        client.force_login(admin)

        with patch(
            'anthias_server.settings.ViewerPublisher.send_to_viewer',
            return_value=None,
        ):
            client.post(
                reverse('anthias_app:settings_save'),
                {
                    'player_name': 'Attempted New Name',
                    'default_duration': '999',
                    'default_streaming_duration': '999',
                    'audio_output': 'hdmi',
                    'date_format': 'mm/dd/yyyy',
                },
            )

        device_settings.load()
        assert device_settings['player_name'] == 'Original Name'
    finally:
        device_settings.conf_file = original_conf_file
        device_settings.load()


@pytest.mark.django_db
def test_settings_save_applies_fields_when_not_fleet_managed(
    tmp_path: Any,
) -> None:
    from anthias_server.settings import settings as device_settings

    original_conf_file = device_settings.conf_file
    try:
        device_settings.conf_file = str(tmp_path / 'anthias.conf')
        device_settings.save()

        admin = _make_admin()
        client = Client()
        client.force_login(admin)

        with patch(
            'anthias_server.settings.ViewerPublisher.send_to_viewer',
            return_value=None,
        ):
            client.post(
                reverse('anthias_app:settings_save'),
                {
                    'player_name': 'New Name',
                    'default_duration': '10',
                    'default_streaming_duration': '300',
                    'audio_output': 'hdmi',
                    'date_format': 'mm/dd/yyyy',
                },
            )

        device_settings.load()
        assert device_settings['player_name'] == 'New Name'
    finally:
        device_settings.conf_file = original_conf_file
        device_settings.load()


# ---------------------------------------------------------------------------
# DeviceSettingsViewV2.patch request.auth enforcement


@pytest.mark.django_db
def test_device_settings_patch_rejects_non_fleet_bearer_token_when_paired() -> (
    None
):
    admin = _make_admin()
    issue_api_token(
        admin, 'pairing', purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    )
    _, other_raw = issue_api_token(admin, 'unrelated integration')

    client = Client()
    response = client.patch(
        '/api/v2/device_settings',
        data=json.dumps({'player_name': 'Hijacked'}),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {other_raw}',
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_device_settings_patch_rejects_session_auth_when_paired() -> None:
    admin = _make_admin()
    issue_api_token(
        admin, 'pairing', purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    )

    client = Client()
    client.force_login(admin)
    response = client.patch(
        '/api/v2/device_settings',
        data=json.dumps({'player_name': 'Hijacked'}),
        content_type='application/json',
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_device_settings_patch_accepts_the_paired_fleet_token(
    tmp_path: Any,
) -> None:
    # A successful PATCH reaches settings.save() — isolate the conf
    # file like test_settings_save_*/test_settings_save_self_password_
    # change_still_works do, so this never writes the real host's
    # ~/.anthias/anthias.conf.
    from anthias_server.settings import settings as device_settings

    original_conf_file = device_settings.conf_file
    try:
        device_settings.conf_file = str(tmp_path / 'anthias.conf')
        device_settings.save()

        admin = _make_admin()
        _, fleet_raw = issue_api_token(
            admin, 'pairing', purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
        )

        client = Client()
        with patch(
            'anthias_server.settings.ViewerPublisher.get_instance',
            return_value=MagicMock(),
        ):
            response = client.patch(
                '/api/v2/device_settings',
                data=json.dumps({'player_name': 'Set By Fleet'}),
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {fleet_raw}',
            )
        assert response.status_code == 200
    finally:
        device_settings.conf_file = original_conf_file
        device_settings.load()


@pytest.mark.django_db
def test_device_settings_patch_unrestricted_when_not_fleet_managed(
    tmp_path: Any,
) -> None:
    from anthias_server.settings import settings as device_settings

    original_conf_file = device_settings.conf_file
    try:
        device_settings.conf_file = str(tmp_path / 'anthias.conf')
        device_settings.save()

        admin = _make_admin()
        client = Client()
        client.force_login(admin)

        with patch(
            'anthias_server.settings.ViewerPublisher.get_instance',
            return_value=MagicMock(),
        ):
            response = client.patch(
                '/api/v2/device_settings',
                data=json.dumps({'player_name': 'Still Local'}),
                content_type='application/json',
            )
        assert response.status_code == 200
    finally:
        device_settings.conf_file = original_conf_file
        device_settings.load()


# ---------------------------------------------------------------------------
# PlayerAPIClient.get_device_settings / update_device_settings


def _make_player(**overrides: Any) -> Player:
    player = Player(
        name='Lobby TV',
        base_url=overrides.pop('base_url', 'http://192.168.1.50:8080'),
    )
    player.set_api_token(_RAW_TOKEN)
    return player


def _response(status_code: int = 200, json_data: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.content = b'{}'
    resp.json.return_value = json_data
    return resp


def test_get_device_settings_calls_correct_endpoint() -> None:
    client = PlayerAPIClient(_make_player())
    payload = {'player_name': 'Remote Player'}

    with patch.object(
        AnthiasSession, 'request', return_value=_response(json_data=payload)
    ) as mock_request:
        result = client.get_device_settings()

    assert result == payload
    method, url = mock_request.call_args.args[:2]
    assert method == 'GET'
    assert url.endswith('/api/v2/device_settings')


def test_update_device_settings_sends_patch_with_json_body() -> None:
    client = PlayerAPIClient(_make_player())

    with patch.object(
        AnthiasSession, 'request', return_value=_response(json_data={})
    ) as mock_request:
        client.update_device_settings({'player_name': 'New Name'})

    method, url = mock_request.call_args.args[:2]
    assert method == 'PATCH'
    assert url.endswith('/api/v2/device_settings')
    assert mock_request.call_args.kwargs['json'] == {'player_name': 'New Name'}


# ---------------------------------------------------------------------------
# fleet.views.player_settings


def _make_fleet_player(**overrides: Any) -> Player:
    player = Player(
        name=overrides.pop('name', 'Lobby TV'),
        base_url=overrides.pop('base_url', 'http://192.168.1.50:8080'),
        **overrides,
    )
    player.set_api_token(_RAW_TOKEN)
    player.save()
    return player


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_settings_get_renders_form_with_fetched_values(
    client: Client,
) -> None:
    player = _make_fleet_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.get_device_settings',
        return_value={'player_name': 'Remote Value', 'default_duration': 15},
    ):
        response = client.get(
            reverse('anthias_fleet:player_settings', args=[player.id])
        )
    assert response.status_code == 200
    assert 'Remote Value' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_settings_get_shows_error_on_unreachable(
    client: Client,
) -> None:
    from anthias_server.fleet.player_client import PlayerUnreachableError

    player = _make_fleet_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.get_device_settings',
        side_effect=PlayerUnreachableError('no route to host'),
    ):
        response = client.get(
            reverse('anthias_fleet:player_settings', args=[player.id])
        )
    assert response.status_code == 200
    assert 'Could not reach this player' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_settings_post_proxies_update(client: Client) -> None:
    player = _make_fleet_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.update_device_settings',
        return_value={},
    ) as mock_update:
        response = client.post(
            reverse('anthias_fleet:player_settings', args=[player.id]),
            {
                'player_name': 'Updated',
                'default_duration': '20',
                'default_streaming_duration': '300',
                'screen_rotation': '90',
                'audio_output': 'hdmi',
                'date_format': 'mm/dd/yyyy',
                'timezone': '',
                'show_splash': 'true',
            },
        )
    assert response.status_code == 302
    mock_update.assert_called_once()
    sent = mock_update.call_args.args[0]
    assert sent['player_name'] == 'Updated'
    assert sent['default_duration'] == 20
    assert sent['screen_rotation'] == 90
    assert sent['show_splash'] is True
    assert sent['debug_logging'] is False
