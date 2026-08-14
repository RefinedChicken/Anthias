"""Tests for the fleet console's views (``anthias_server.fleet.views``).

``ROOT_URLCONF`` defaults to the *player* urlconf during a normal test
run (``ANTHIAS_SERVICE`` isn't set), so every test that needs
``reverse('anthias_fleet:...')`` or hits a fleet URL through the test
``Client`` carries
``@pytest.mark.urls('anthias_server.django_project.fleet_urls')`` —
pytest-django's per-test urlconf override, not a real process-level
``ANTHIAS_SERVICE=fleet``.

Every ``PlayerAPIClient`` method is mocked — these tests never hit
real network I/O, matching ``tests/test_player_api_client.py``'s own
convention of testing the client in isolation from what calls it.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from anthias_server.fleet.models import (
    AssetPushJob,
    AssetPushJobTarget,
    Player,
    PlayerGroup,
    PlaylistTemplate,
    PlaylistTemplateItem,
    PlaylistTemplatePlacement,
    TemplateApplicationJob,
    TemplateApplicationJobTarget,
)
from anthias_server.fleet.player_client import (
    PlayerAPIError,
    PlayerUnreachableError,
)

_FLEET_URLCONF = 'anthias_server.django_project.fleet_urls'
_RAW_TOKEN = 'ant_fixture-fleet-view-token'  # NOSONAR


@pytest.fixture
def client() -> Client:
    return Client()


def _make_player(**overrides: Any) -> Player:
    player = Player(
        name=overrides.pop('name', 'Lobby TV'),
        base_url=overrides.pop('base_url', 'http://192.168.1.50:8080'),
        **overrides,
    )
    player.set_api_token(_RAW_TOKEN)
    player.save()
    return player


# ---------------------------------------------------------------------------
# Player list


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_list_renders(client: Client) -> None:
    _make_player(name='Lobby TV')
    response = client.get(reverse('anthias_fleet:player_list'))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'Lobby TV' in body


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_table_partial_renders(client: Client) -> None:
    _make_player(name='Warehouse TV', is_reachable=False)
    response = client.get(reverse('anthias_fleet:player_table'))
    assert response.status_code == 200
    assert 'Warehouse TV' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_list_renders_empty_state(client: Client) -> None:
    response = client.get(reverse('anthias_fleet:player_list'))
    assert response.status_code == 200
    assert 'No players yet' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_edit_get_renders_form(client: Client) -> None:
    player = _make_player(name='Lobby TV')
    response = client.get(
        reverse('anthias_fleet:player_edit', args=[player.id])
    )
    assert response.status_code == 200
    assert 'Lobby TV' in response.content.decode()


# ---------------------------------------------------------------------------
# Registration


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_get_renders_form(client: Client) -> None:
    response = client.get(reverse('anthias_fleet:player_new'))
    assert response.status_code == 200
    assert 'API token' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_get_renders_group_dropdown_with_existing_group(
    client: Client,
) -> None:
    """Exercises the dropdown's ``{% if form_values.group == ... %}``
    branch on a plain GET (``form_values`` isn't in context yet) — a
    regression guard for the VariableDoesNotExist trap a chained
    ``|default:`` filter argument fell into elsewhere in this app (see
    the ``_error.html`` / group_form.html fixes in this same change)."""
    PlayerGroup.objects.create(name='Lobby')
    response = client.get(reverse('anthias_fleet:player_new'))
    assert response.status_code == 200
    assert 'Lobby' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_post_success_saves_player_populated_from_get_info(
    client: Client,
) -> None:
    info = {
        'anthias_version': 'v2026.8.0',
        'mac_address': 'aa:bb:cc:dd:ee:ff',
        'device_model': 'Raspberry Pi 4',
    }
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.get_info',
        return_value=info,
    ):
        response = client.post(
            reverse('anthias_fleet:player_new'),
            {
                'name': 'Lobby TV',
                'base_url': 'http://192.168.1.50:8080',
                'api_token': _RAW_TOKEN,
            },
        )

    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_fleet:player_list')

    player = Player.objects.get(name='Lobby TV')
    assert player.anthias_version == 'v2026.8.0'
    assert player.mac_address == 'aa:bb:cc:dd:ee:ff'
    assert player.device_model == 'Raspberry Pi 4'
    assert player.is_reachable is True
    assert player.last_reachability_check is not None
    # api_token_encrypted must never hold the plaintext token.
    assert _RAW_TOKEN not in player.api_token_encrypted


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_post_unreachable_does_not_save_a_row(
    client: Client,
) -> None:
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.get_info',
        side_effect=PlayerUnreachableError('connection refused'),
    ):
        response = client.post(
            reverse('anthias_fleet:player_new'),
            {
                'name': 'Unreachable TV',
                'base_url': 'http://192.168.1.99:8080',
                'api_token': _RAW_TOKEN,
            },
        )

    assert response.status_code == 200
    assert not Player.objects.filter(name='Unreachable TV').exists()
    assert 'connection refused' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_post_api_error_does_not_save_a_row(
    client: Client,
) -> None:
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.get_info',
        side_effect=PlayerAPIError(401, 'Unauthorized'),
    ):
        response = client.post(
            reverse('anthias_fleet:player_new'),
            {
                'name': 'Bad Token TV',
                'base_url': 'http://192.168.1.98:8080',
                'api_token': 'ant_wrong-token',
            },
        )

    assert response.status_code == 200
    assert not Player.objects.filter(name='Bad Token TV').exists()
    assert '401' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_post_rejects_missing_fields(client: Client) -> None:
    response = client.post(
        reverse('anthias_fleet:player_new'),
        {'name': '', 'base_url': '', 'api_token': ''},
    )
    assert response.status_code == 200
    assert Player.objects.count() == 0


# ---------------------------------------------------------------------------
# Drill-down


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_detail_renders_and_proxies_list_assets(client: Client) -> None:
    player = _make_player()
    raw_assets = [
        {
            'asset_id': 'abc123',
            'name': 'Welcome sign',
            'uri': 'https://example.com',
            'mimetype': 'webpage',
            'is_enabled': True,
            'is_processing': False,
            'duration': 10,
            'play_order': 0,
            'start_date': '2026-01-01T00:00:00Z',
            'end_date': '2027-01-01T00:00:00Z',
            'play_days': [1, 2, 3, 4, 5, 6, 7],
        }
    ]
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.list_assets',
        return_value=raw_assets,
    ) as mock_list:
        response = client.get(
            reverse('anthias_fleet:player_detail', args=[player.id])
        )

    assert response.status_code == 200
    assert 'Welcome sign' in response.content.decode()
    mock_list.assert_called_once()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_detail_renders_app_store_index_meta_tag(
    client: Client,
) -> None:
    """The Add-asset modal's Apps tab (appsTab(), reused from the
    player app's own bundle) reads the store index URL off this meta
    tag client-side — see fleet.views._player_detail_context. Unlike
    every other fleet page, the drill-down must carry it."""
    from django.conf import settings as django_settings

    player = _make_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.list_assets',
        return_value=[],
    ):
        response = client.get(
            reverse('anthias_fleet:player_detail', args=[player.id])
        )

    assert response.status_code == 200
    body = response.content.decode()
    assert 'name="anthias-app-store-index"' in body
    assert django_settings.APP_STORE_INDEX_URL in body


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_detail_shows_fetch_error_when_player_unreachable(
    client: Client,
) -> None:
    player = _make_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.list_assets',
        side_effect=PlayerUnreachableError('timed out'),
    ):
        response = client.get(
            reverse('anthias_fleet:player_detail', args=[player.id])
        )

    assert response.status_code == 200
    assert 'timed out' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_create_proxies_with_expected_payload(
    client: Client,
) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.create_asset',
            return_value={},
        ) as mock_create,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_create', args=[player.id]),
            {
                'uri': 'https://example.com/sign.png',
                'name': 'Sign',
                'mimetype': 'image',
                'duration': '15',
            },
        )

    assert response.status_code == 302
    mock_create.assert_called_once()
    sent = mock_create.call_args.args[0]
    assert sent['uri'] == 'https://example.com/sign.png'
    assert sent['name'] == 'Sign'
    assert sent['mimetype'] == 'image'
    assert sent['duration'] == 15
    assert sent['is_enabled'] is True


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_upload_uploads_then_creates(client: Client) -> None:
    player = _make_player()
    upload = SimpleUploadedFile(
        'photo.jpg', b'\xff\xd8\xff', content_type='image/jpeg'
    )
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.upload_file',
            return_value={
                'uri': '/data/.anthias/assets/abc123.tmp',
                'ext': '.jpg',
            },
        ) as mock_upload,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.create_asset',
            return_value={},
        ) as mock_create,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_upload', args=[player.id]),
            {'file_upload': upload, 'name': 'Lobby photo', 'duration': '20'},
        )

    assert response.status_code == 302
    mock_upload.assert_called_once()
    filename, content, content_type = mock_upload.call_args.args
    assert filename == 'photo.jpg'
    assert content == b'\xff\xd8\xff'
    assert content_type == 'image/jpeg'

    mock_create.assert_called_once()
    sent = mock_create.call_args.args[0]
    assert sent['uri'] == '/data/.anthias/assets/abc123.tmp'
    assert sent['ext'] == '.jpg'
    assert sent['name'] == 'Lobby photo'
    assert sent['mimetype'] == 'image'
    assert sent['duration'] == 20
    assert sent['is_enabled'] is True


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_upload_forces_zero_duration_for_video(
    client: Client,
) -> None:
    player = _make_player()
    upload = SimpleUploadedFile(
        'clip.mp4', b'\x00\x00\x00', content_type='video/mp4'
    )
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.upload_file',
            return_value={'uri': '/data/.anthias/assets/def456.tmp'},
        ),
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.create_asset',
            return_value={},
        ) as mock_create,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_upload', args=[player.id]),
            {'file_upload': upload, 'duration': '25'},
        )

    assert response.status_code == 302
    sent = mock_create.call_args.args[0]
    assert sent['mimetype'] == 'video'
    assert sent['duration'] == 0
    # No 'ext' key from upload_file() this time — omitted, not sent as
    # a literal null (CreateAssetSerializerV2's ext field isn't
    # nullable).
    assert 'ext' not in sent


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_upload_rejects_non_media_file(client: Client) -> None:
    player = _make_player()
    upload = SimpleUploadedFile(
        'notes.txt', b'hello', content_type='text/plain'
    )
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.upload_file',
        ) as mock_upload,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_upload', args=[player.id]),
            {'file_upload': upload},
            follow=True,
        )

    assert response.status_code == 200
    assert 'Invalid file type' in response.content.decode()
    mock_upload.assert_not_called()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_upload_rejects_missing_file(client: Client) -> None:
    player = _make_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.list_assets',
        return_value=[],
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_upload', args=[player.id]),
            {},
            follow=True,
        )

    assert response.status_code == 200
    assert 'No file uploaded' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_create_app_proxies_with_expected_payload(
    client: Client,
) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.create_asset',
            return_value={},
        ) as mock_create,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_create_app', args=[player.id]),
            {
                'app_id': 'weather',
                'app_uri': 'https://weather.srly.io/launch?city=nyc',
                'manifest_url': 'https://weather.srly.io/manifest.json',
                'manifest_version': '1',
                'name': 'Weather',
                'app_values': '{"city": "nyc"}',
                'refresh_interval_s': '300',
            },
        )

    assert response.status_code == 302
    mock_create.assert_called_once()
    sent = mock_create.call_args.args[0]
    assert sent['uri'] == 'https://weather.srly.io/launch?city=nyc'
    assert sent['name'] == 'Weather'
    assert sent['mimetype'] == 'webpage'
    assert sent['is_enabled'] is True
    assert sent['metadata']['app']['id'] == 'weather'
    assert sent['metadata']['app']['manifest_url'] == (
        'https://weather.srly.io/manifest.json'
    )
    assert sent['metadata']['app']['values'] == {'city': 'nyc'}
    assert sent['refresh_interval_s'] == 300


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_create_app_rejects_missing_fields(
    client: Client,
) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.create_asset',
        ) as mock_create,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_create_app', args=[player.id]),
            {'app_id': '', 'app_uri': ''},
            follow=True,
        )

    assert response.status_code == 200
    assert 'invalid app data' in response.content.decode()
    mock_create.assert_not_called()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_create_app_rejects_disallowed_host(
    client: Client,
) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.create_asset',
        ) as mock_create,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse('anthias_fleet:player_asset_create_app', args=[player.id]),
            {
                'app_id': 'evil',
                'app_uri': 'https://evil.example.com/launch',
            },
            follow=True,
        )

    assert response.status_code == 200
    assert 'not from a recognised app store' in response.content.decode()
    mock_create.assert_not_called()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_toggle_sends_target_state(client: Client) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.update_asset',
            return_value={},
        ) as mock_update,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_toggle',
                args=[player.id, 'abc123'],
            ),
            {'is_enabled': 'true'},
        )

    assert response.status_code == 302
    mock_update.assert_called_once_with('abc123', {'is_enabled': True})


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_delete_proxies_to_client(client: Client) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.delete_asset',
            return_value=None,
        ) as mock_delete,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_delete',
                args=[player.id, 'abc123'],
            )
        )

    assert response.status_code == 302
    mock_delete.assert_called_once_with('abc123')


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_move_swaps_adjacent_play_order(client: Client) -> None:
    player = _make_player()
    raw_assets = [
        {
            'asset_id': 'a',
            'name': 'A',
            'is_enabled': True,
            'is_processing': False,
            'play_order': 0,
        },
        {
            'asset_id': 'b',
            'name': 'B',
            'is_enabled': True,
            'is_processing': False,
            'play_order': 1,
        },
    ]
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=raw_assets,
        ),
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.reorder_assets',
            return_value=None,
        ) as mock_reorder,
    ):
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_move',
                args=[player.id, 'b', 'up'],
            )
        )

    assert response.status_code == 302
    mock_reorder.assert_called_once_with(['b', 'a'])


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_control_posts_and_redirects(client: Client) -> None:
    player = _make_player()
    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.control',
        return_value=None,
    ) as mock_control:
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_control',
                args=[player.id, 'next'],
            )
        )

    assert response.status_code == 302
    assert response['Location'] == reverse(
        'anthias_fleet:player_detail', args=[player.id]
    )
    mock_control.assert_called_once_with('next')


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_control_rejects_unknown_command(client: Client) -> None:
    player = _make_player()
    response = client.post(
        reverse(
            'anthias_fleet:player_asset_control',
            args=[player.id, 'sideways'],
        )
    )
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_assets_bulk_action_loops_over_selection(
    client: Client,
) -> None:
    player = _make_player()
    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.update_asset',
            return_value={},
        ) as mock_update,
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse(
                'anthias_fleet:player_assets_bulk_action', args=[player.id]
            ),
            {'action': 'disable', 'ids': 'a,b,c'},
        )

    assert response.status_code == 302
    assert mock_update.call_count == 3
    called_ids = {c.args[0] for c in mock_update.call_args_list}
    assert called_ids == {'a', 'b', 'c'}
    for c in mock_update.call_args_list:
        assert c.args[1] == {'is_enabled': False}


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_assets_bulk_action_reports_partial_failure(
    client: Client,
) -> None:
    player = _make_player()

    def _side_effect(asset_id: str, _data: dict[str, Any]) -> dict[str, Any]:
        if asset_id == 'bad':
            raise PlayerAPIError(404, 'Not found')
        return {}

    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.update_asset',
            side_effect=_side_effect,
        ),
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.list_assets',
            return_value=[],
        ),
    ):
        response = client.post(
            reverse(
                'anthias_fleet:player_assets_bulk_action', args=[player.id]
            ),
            {'action': 'enable', 'ids': 'good,bad'},
            follow=True,
        )

    assert response.status_code == 200
    body = response.content.decode()
    assert '1 asset' in body
    assert '1 failed' in body


# ---------------------------------------------------------------------------
# Fleet-wide bulk player actions


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_players_bulk_action_loops_and_reports_per_player_outcome(
    client: Client,
) -> None:
    ok_player = _make_player(name='OK TV', base_url='http://10.0.0.1:8080')
    bad_player = _make_player(name='Bad TV', base_url='http://10.0.0.99:8080')

    def _reboot(self: Any) -> None:
        if '10.0.0.99' in self._base_url:
            raise PlayerUnreachableError('no route to host')

    with mock.patch(
        'anthias_server.fleet.views.PlayerAPIClient.reboot',
        autospec=True,
        side_effect=_reboot,
    ):
        response = client.post(
            reverse('anthias_fleet:players_bulk_action'),
            {
                'action': 'reboot',
                'ids': f'{ok_player.id},{bad_player.id}',
            },
            follow=True,
        )

    assert response.status_code == 200
    body = response.content.decode()
    assert 'OK TV' in body
    assert 'Bad TV' in body


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_players_bulk_action_rejects_unknown_action(client: Client) -> None:
    player = _make_player()
    response = client.post(
        reverse('anthias_fleet:players_bulk_action'),
        {'action': 'nonsense', 'ids': str(player.id)},
        follow=True,
    )
    assert response.status_code == 200
    assert 'Unknown bulk action' in response.content.decode()


# ---------------------------------------------------------------------------
# Player edit / delete


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_edit_updates_fields_without_touching_token_when_blank(
    client: Client,
) -> None:
    player = _make_player(name='Old name')
    original_encrypted = player.api_token_encrypted

    response = client.post(
        reverse('anthias_fleet:player_edit', args=[player.id]),
        {
            'name': 'New name',
            'base_url': 'http://192.168.1.51:8080',
            'api_token': '',
        },
    )

    assert response.status_code == 302
    player.refresh_from_db()
    assert player.name == 'New name'
    assert player.base_url == 'http://192.168.1.51:8080'
    assert player.api_token_encrypted == original_encrypted


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_delete_removes_row(client: Client) -> None:
    player = _make_player()
    response = client.post(
        reverse('anthias_fleet:player_delete', args=[player.id])
    )
    assert response.status_code == 302
    assert not Player.objects.filter(pk=player.id).exists()


# ---------------------------------------------------------------------------
# Groups


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_new_creates_group(client: Client) -> None:
    response = client.post(
        reverse('anthias_fleet:group_new'),
        {'name': 'Lobby', 'description': 'Front desk screens'},
    )
    assert response.status_code == 302
    assert PlayerGroup.objects.filter(name='Lobby').exists()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_new_rejects_duplicate_name(client: Client) -> None:
    PlayerGroup.objects.create(name='Lobby')
    response = client.post(
        reverse('anthias_fleet:group_new'),
        {'name': 'Lobby', 'description': ''},
    )
    assert response.status_code == 200
    assert PlayerGroup.objects.filter(name='Lobby').count() == 1


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_list_renders_with_player_counts(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    _make_player(name='Lobby TV', group=group)
    response = client.get(reverse('anthias_fleet:group_list'))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'Lobby' in body


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_delete_removes_group_and_ungroups_players(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    player = _make_player(group=group)

    response = client.post(
        reverse('anthias_fleet:group_delete', args=[group.id])
    )

    assert response.status_code == 302
    assert not PlayerGroup.objects.filter(pk=group.id).exists()
    player.refresh_from_db()
    assert player.group_id is None


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_list_renders_empty_state(client: Client) -> None:
    response = client.get(reverse('anthias_fleet:group_list'))
    assert response.status_code == 200
    assert 'No groups yet' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_edit_get_renders_form_and_post_updates(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')

    get_response = client.get(
        reverse('anthias_fleet:group_edit', args=[group.id])
    )
    assert get_response.status_code == 200
    assert 'Lobby' in get_response.content.decode()

    post_response = client.post(
        reverse('anthias_fleet:group_edit', args=[group.id]),
        {'name': 'Warehouse', 'description': 'Back of house'},
    )
    assert post_response.status_code == 302
    group.refresh_from_db()
    assert group.name == 'Warehouse'
    assert group.description == 'Back of house'


# ---------------------------------------------------------------------------
# Bulk asset push


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_push_creates_job_and_targets_and_enqueues(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    source = _make_player(name='Source', group=group)
    target_a = _make_player(name='A', group=group)
    target_b = _make_player(name='B', group=group)
    # Different group — must never receive this push.
    other_group = PlayerGroup.objects.create(name='Warehouse')
    _make_player(name='C', group=other_group)

    with mock.patch(
        'anthias_server.fleet.views.push_asset_to_group.delay'
    ) as mock_delay:
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_push',
                args=[source.id, 'asset-1'],
            ),
            {'group_id': str(group.id)},
        )

    assert response.status_code == 302
    job = AssetPushJob.objects.get()
    assert job.group_id == group.id
    assert job.source_player_id == source.id
    assert job.source_asset_id == 'asset-1'
    mock_delay.assert_called_once_with(job.id)

    target_player_ids = set(job.targets.values_list('player_id', flat=True))
    assert target_player_ids == {target_a.id, target_b.id}
    assert response['Location'] == reverse(
        'anthias_fleet:push_job_status', args=[job.id]
    )


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_push_defaults_to_source_players_own_group(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    source = _make_player(name='Source', group=group)
    _make_player(name='A', group=group)

    with mock.patch('anthias_server.fleet.views.push_asset_to_group.delay'):
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_push',
                args=[source.id, 'asset-1'],
            ),
        )

    assert response.status_code == 302
    job = AssetPushJob.objects.get()
    assert job.group_id == group.id


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_push_errors_with_no_group_chosen(
    client: Client,
) -> None:
    source = _make_player(name='Source')  # no group

    with mock.patch(
        'anthias_server.fleet.views.push_asset_to_group.delay'
    ) as mock_delay:
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_push',
                args=[source.id, 'asset-1'],
            ),
        )

    assert response.status_code == 302
    assert not AssetPushJob.objects.exists()
    mock_delay.assert_not_called()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_asset_push_noop_when_group_has_no_other_members(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    source = _make_player(name='Source', group=group)

    with mock.patch(
        'anthias_server.fleet.views.push_asset_to_group.delay'
    ) as mock_delay:
        response = client.post(
            reverse(
                'anthias_fleet:player_asset_push',
                args=[source.id, 'asset-1'],
            ),
            {'group_id': str(group.id)},
        )

    assert response.status_code == 302
    assert not AssetPushJob.objects.exists()
    mock_delay.assert_not_called()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_push_job_status_renders(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    source = _make_player(name='Source', group=group)
    target = _make_player(name='A', group=group)
    job = AssetPushJob.objects.create(
        group=group, source_player=source, source_asset_id='asset-1'
    )
    AssetPushJobTarget.objects.create(job=job, player=target)

    response = client.get(
        reverse('anthias_fleet:push_job_status', args=[job.id])
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'Lobby' in body
    assert 'A' in body


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_push_job_status_partial_stops_polling_when_all_done(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    source = _make_player(name='Source', group=group)
    target = _make_player(name='A', group=group)
    job = AssetPushJob.objects.create(
        group=group, source_player=source, source_asset_id='asset-1'
    )
    AssetPushJobTarget.objects.create(
        job=job, player=target, status=AssetPushJobTarget.STATUS_SUCCESS
    )

    response = client.get(
        reverse('anthias_fleet:push_job_status_partial', args=[job.id])
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'hx-trigger' not in body


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_push_job_status_partial_keeps_polling_while_pending(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    source = _make_player(name='Source', group=group)
    target = _make_player(name='A', group=group)
    job = AssetPushJob.objects.create(
        group=group, source_player=source, source_asset_id='asset-1'
    )
    AssetPushJobTarget.objects.create(job=job, player=target)

    response = client.get(
        reverse('anthias_fleet:push_job_status_partial', args=[job.id])
    )
    assert response.status_code == 200
    assert 'hx-trigger="every 3s"' in response.content.decode()


# ---------------------------------------------------------------------------
# Playlist templates


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_list_renders(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    PlaylistTemplate.objects.create(name='Menu', group=group)
    response = client.get(reverse('anthias_fleet:template_list'))
    assert response.status_code == 200
    assert 'Menu' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_new_get_renders_form_with_groups(client: Client) -> None:
    PlayerGroup.objects.create(name='Lobby')
    response = client.get(reverse('anthias_fleet:template_new'))
    assert response.status_code == 200
    assert 'Lobby' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_new_post_creates_and_redirects_to_detail(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    response = client.post(
        reverse('anthias_fleet:template_new'),
        {
            'name': 'Menu',
            'description': 'Daily specials',
            'group': str(group.id),
        },
    )
    assert response.status_code == 302
    tmpl = PlaylistTemplate.objects.get(name='Menu')
    assert tmpl.group_id == group.id
    assert response['Location'] == reverse(
        'anthias_fleet:template_detail', args=[tmpl.id]
    )


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_new_post_rejects_missing_group(client: Client) -> None:
    response = client.post(
        reverse('anthias_fleet:template_new'), {'name': 'Menu', 'group': ''}
    )
    assert response.status_code == 200
    assert not PlaylistTemplate.objects.filter(name='Menu').exists()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_new_post_rejects_duplicate_name(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    PlaylistTemplate.objects.create(name='Menu', group=group)
    response = client.post(
        reverse('anthias_fleet:template_new'),
        {'name': 'Menu', 'group': str(group.id)},
    )
    assert response.status_code == 200
    assert PlaylistTemplate.objects.filter(name='Menu').count() == 1


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_edit_updates_name_and_description_not_group(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)

    response = client.post(
        reverse('anthias_fleet:template_edit', args=[tmpl.id]),
        {'name': 'Daily Menu', 'description': 'Updated'},
    )
    assert response.status_code == 302
    tmpl.refresh_from_db()
    assert tmpl.name == 'Daily Menu'
    assert tmpl.description == 'Updated'
    assert tmpl.group_id == group.id


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_delete_removes_template(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)

    response = client.post(
        reverse('anthias_fleet:template_delete', args=[tmpl.id])
    )
    assert response.status_code == 302
    assert not PlaylistTemplate.objects.filter(pk=tmpl.pk).exists()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_detail_renders_items(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    PlaylistTemplateItem.objects.create(
        template=tmpl,
        name='Board',
        uri='https://example.com',
        mimetype='webpage',
    )
    response = client.get(
        reverse('anthias_fleet:template_detail', args=[tmpl.id])
    )
    assert response.status_code == 200
    assert 'Board' in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_item_new_creates_item(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)

    response = client.post(
        reverse('anthias_fleet:template_item_new', args=[tmpl.id]),
        {
            'name': 'Board',
            'uri': 'https://example.com/board',
            'mimetype': 'webpage',
            'duration': '15',
            'is_enabled': 'true',
        },
    )
    assert response.status_code == 302
    item = PlaylistTemplateItem.objects.get(template=tmpl)
    assert item.name == 'Board'
    assert item.uri == 'https://example.com/board'
    assert item.duration == 15
    assert item.is_enabled is True
    assert item.play_days == ''  # all 7 selected in the form's default


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_item_new_rejects_invalid_url(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)

    response = client.post(
        reverse('anthias_fleet:template_item_new', args=[tmpl.id]),
        {'name': 'Board', 'uri': 'not-a-url'},
    )
    assert response.status_code == 200
    assert not PlaylistTemplateItem.objects.filter(template=tmpl).exists()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_item_new_stores_partial_play_days_selection(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)

    response = client.post(
        reverse('anthias_fleet:template_item_new', args=[tmpl.id]),
        {
            'name': 'Board',
            'uri': 'https://example.com/board',
            'mimetype': 'webpage',
            'play_days': ['1', '3', '5'],
        },
    )
    assert response.status_code == 302
    item = PlaylistTemplateItem.objects.get(template=tmpl)
    assert item.get_play_days() == [1, 3, 5]


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_item_edit_updates_fields(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    item = PlaylistTemplateItem.objects.create(
        template=tmpl,
        name='Board',
        uri='https://example.com',
        mimetype='webpage',
    )

    response = client.post(
        reverse('anthias_fleet:template_item_edit', args=[tmpl.id, item.id]),
        {
            'name': 'Updated Board',
            'uri': 'https://example.com/updated',
            'mimetype': 'webpage',
            'duration': '20',
            'is_enabled': 'true',
        },
    )
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.name == 'Updated Board'
    assert item.uri == 'https://example.com/updated'
    assert item.duration == 20


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_item_delete_orphans_placement_instead_of_deleting_it(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    item = PlaylistTemplateItem.objects.create(
        template=tmpl,
        name='Board',
        uri='https://example.com',
        mimetype='webpage',
    )
    player = _make_player(name='A', group=group)
    placement = PlaylistTemplatePlacement.objects.create(
        template=tmpl, item=item, player=player, remote_asset_id='remote-1'
    )

    response = client.post(
        reverse('anthias_fleet:template_item_delete', args=[tmpl.id, item.id])
    )
    assert response.status_code == 302
    assert not PlaylistTemplateItem.objects.filter(pk=item.pk).exists()
    placement.refresh_from_db()
    assert placement.item_id is None


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_item_move_swaps_order(client: Client) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    item_a = PlaylistTemplateItem.objects.create(
        template=tmpl,
        name='A',
        uri='https://example.com/a',
        mimetype='webpage',
        order=0,
    )
    item_b = PlaylistTemplateItem.objects.create(
        template=tmpl,
        name='B',
        uri='https://example.com/b',
        mimetype='webpage',
        order=1,
    )

    response = client.post(
        reverse(
            'anthias_fleet:template_item_move', args=[tmpl.id, item_b.id, 'up']
        )
    )
    assert response.status_code == 302
    item_a.refresh_from_db()
    item_b.refresh_from_db()
    assert item_b.order < item_a.order


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_apply_creates_job_and_targets_and_enqueues(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    member_a = _make_player(name='A', group=group)
    member_b = _make_player(name='B', group=group)

    with mock.patch(
        'anthias_server.fleet.views.apply_playlist_template.delay'
    ) as mock_delay:
        response = client.post(
            reverse('anthias_fleet:template_apply', args=[tmpl.id])
        )

    assert response.status_code == 302
    job = TemplateApplicationJob.objects.get(template=tmpl)
    mock_delay.assert_called_once_with(job.id)
    target_player_ids = set(job.targets.values_list('player_id', flat=True))
    assert target_player_ids == {member_a.id, member_b.id}
    assert response['Location'] == reverse(
        'anthias_fleet:template_job_status', args=[job.id]
    )


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_apply_noop_when_group_has_no_players(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)

    with mock.patch(
        'anthias_server.fleet.views.apply_playlist_template.delay'
    ) as mock_delay:
        response = client.post(
            reverse('anthias_fleet:template_apply', args=[tmpl.id])
        )

    assert response.status_code == 302
    assert not TemplateApplicationJob.objects.exists()
    mock_delay.assert_not_called()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_job_status_partial_stops_polling_when_all_done(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    player = _make_player(name='A', group=group)
    job = TemplateApplicationJob.objects.create(template=tmpl)
    TemplateApplicationJobTarget.objects.create(
        job=job,
        player=player,
        status=TemplateApplicationJobTarget.STATUS_SUCCESS,
    )

    response = client.get(
        reverse('anthias_fleet:template_job_status_partial', args=[job.id])
    )
    assert response.status_code == 200
    assert 'hx-trigger' not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_template_job_status_partial_keeps_polling_while_pending(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    player = _make_player(name='A', group=group)
    job = TemplateApplicationJob.objects.create(template=tmpl)
    TemplateApplicationJobTarget.objects.create(job=job, player=player)

    response = client.get(
        reverse('anthias_fleet:template_job_status_partial', args=[job.id])
    )
    assert response.status_code == 200
    assert 'hx-trigger="every 3s"' in response.content.decode()


# ---------------------------------------------------------------------------
# Group-membership hooks: join/leave template materialization


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_new_into_a_group_enqueues_its_templates(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    PlaylistTemplate.objects.create(name='Menu', group=group)

    with (
        mock.patch(
            'anthias_server.fleet.views.PlayerAPIClient.get_info',
            return_value={},
        ),
        mock.patch(
            'anthias_server.fleet.views.apply_playlist_template_to_player.delay'
        ) as mock_delay,
    ):
        response = client.post(
            reverse('anthias_fleet:player_new'),
            {
                'name': 'New TV',
                'base_url': 'http://192.168.1.60:8080',
                'api_token': _RAW_TOKEN,
                'group': str(group.id),
            },
        )

    assert response.status_code == 302
    mock_delay.assert_called_once()
    player = Player.objects.get(name='New TV')
    job_target = TemplateApplicationJobTarget.objects.get(player=player)
    assert mock_delay.call_args.args[0] == job_target.id


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_edit_group_change_untracks_old_placements(
    client: Client,
) -> None:
    old_group = PlayerGroup.objects.create(name='Lobby')
    new_group = PlayerGroup.objects.create(name='Warehouse')
    old_tmpl = PlaylistTemplate.objects.create(name='Menu', group=old_group)
    old_item = PlaylistTemplateItem.objects.create(
        template=old_tmpl,
        name='Board',
        uri='https://example.com',
        mimetype='webpage',
    )
    player = _make_player(name='A', group=old_group)
    PlaylistTemplatePlacement.objects.create(
        template=old_tmpl,
        item=old_item,
        player=player,
        remote_asset_id='remote-1',
    )

    with mock.patch(
        'anthias_server.fleet.views.apply_playlist_template_to_player.delay'
    ):
        response = client.post(
            reverse('anthias_fleet:player_edit', args=[player.id]),
            {
                'name': 'A',
                'base_url': player.base_url,
                'group': str(new_group.id),
            },
        )

    assert response.status_code == 302
    assert not PlaylistTemplatePlacement.objects.filter(
        template=old_tmpl, player=player
    ).exists()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_player_edit_same_group_does_not_untrack_placements(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    tmpl = PlaylistTemplate.objects.create(name='Menu', group=group)
    item = PlaylistTemplateItem.objects.create(
        template=tmpl,
        name='Board',
        uri='https://example.com',
        mimetype='webpage',
    )
    player = _make_player(name='A', group=group)
    PlaylistTemplatePlacement.objects.create(
        template=tmpl, item=item, player=player, remote_asset_id='remote-1'
    )

    with mock.patch(
        'anthias_server.fleet.views.apply_playlist_template_to_player.delay'
    ) as mock_delay:
        response = client.post(
            reverse('anthias_fleet:player_edit', args=[player.id]),
            {
                'name': 'A',
                'base_url': player.base_url,
                'group': str(group.id),
            },
        )

    assert response.status_code == 302
    mock_delay.assert_not_called()
    assert PlaylistTemplatePlacement.objects.filter(
        template=tmpl, player=player
    ).exists()


@pytest.mark.django_db
@pytest.mark.urls(_FLEET_URLCONF)
def test_group_list_warns_about_templates_before_delete(
    client: Client,
) -> None:
    group = PlayerGroup.objects.create(name='Lobby')
    PlaylistTemplate.objects.create(name='Menu', group=group)
    response = client.get(reverse('anthias_fleet:group_list'))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'This also deletes 1 template' in body
