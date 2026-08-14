"""Tests for PlayerAPIClient — the fleet server's outbound HTTP client
for a registered player's v2 REST API."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from anthias_common.http import AnthiasSession
from anthias_server.fleet.models import Player
from anthias_server.fleet.player_client import (
    PlayerAPIClient,
    PlayerAPIError,
    PlayerUnreachableError,
)

_RAW_TOKEN = 'ant_fixture-client-token'  # NOSONAR


def _make_player(**overrides: Any) -> Player:
    player = Player(
        name='Lobby TV',
        base_url=overrides.pop('base_url', 'http://192.168.1.50:8080/'),
        skip_ssl_verify=overrides.pop('skip_ssl_verify', False),
    )
    player.set_api_token(overrides.pop('api_token', _RAW_TOKEN))
    return player


def _response(
    status_code: int = 200,
    json_data: Any = None,
    content: bytes = b'{}',
    text: str = '',
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.content = content
    resp.text = text
    resp.json.return_value = json_data
    return resp


def test_base_url_trailing_slash_is_stripped() -> None:
    player = _make_player(base_url='http://192.168.1.50:8080/')
    client = PlayerAPIClient(player)

    with patch.object(
        AnthiasSession, 'request', return_value=_response(json_data={})
    ) as mock_request:
        client.get_info()

    called_url = mock_request.call_args.args[1]
    assert called_url == 'http://192.168.1.50:8080/api/v2/info'


def test_get_info_returns_parsed_json() -> None:
    client = PlayerAPIClient(_make_player())
    payload = {'anthias_version': 'v2026.8.0', 'mac_address': 'aa:bb'}

    with patch.object(
        AnthiasSession, 'request', return_value=_response(json_data=payload)
    ):
        assert client.get_info() == payload


def test_authorization_header_carries_decrypted_token() -> None:
    client = PlayerAPIClient(_make_player(api_token='ant_specific-token'))

    fake_session = MagicMock()
    fake_session.headers = {}
    fake_session.request.return_value = _response(json_data={})

    with patch(
        'anthias_server.fleet.player_client.AnthiasSession',
        return_value=fake_session,
    ):
        client.get_info()

    assert fake_session.headers['Authorization'] == 'Bearer ant_specific-token'


def test_network_failure_raises_player_unreachable_error() -> None:
    client = PlayerAPIClient(_make_player())

    with (
        patch.object(
            AnthiasSession,
            'request',
            side_effect=requests.ConnectionError('connection refused'),
        ),
        pytest.raises(PlayerUnreachableError),
    ):
        client.get_info()


def test_timeout_raises_player_unreachable_error() -> None:
    client = PlayerAPIClient(_make_player())

    with (
        patch.object(
            AnthiasSession,
            'request',
            side_effect=requests.Timeout('timed out'),
        ),
        pytest.raises(PlayerUnreachableError),
    ):
        client.get_info()


def test_error_status_raises_player_api_error_with_status_code() -> None:
    client = PlayerAPIClient(_make_player())

    with (
        patch.object(
            AnthiasSession,
            'request',
            return_value=_response(status_code=401, text='Unauthorized'),
        ),
        pytest.raises(PlayerAPIError) as exc_info,
    ):
        client.get_info()

    assert exc_info.value.status_code == 401


def test_upload_file_posts_multipart_and_returns_parsed_json() -> None:
    client = PlayerAPIClient(_make_player())
    payload = {'uri': '/data/.anthias/assets/abc.tmp', 'ext': '.jpg'}

    with patch.object(
        AnthiasSession, 'request', return_value=_response(json_data=payload)
    ) as mock_request:
        result = client.upload_file('photo.jpg', b'\xff\xd8\xff', 'image/jpeg')

    assert result == payload
    method, url = mock_request.call_args.args[:2]
    assert method == 'POST'
    assert url.endswith('/api/v2/file_asset')
    files = mock_request.call_args.kwargs['files']
    assert files == {
        'file_upload': ('photo.jpg', b'\xff\xd8\xff', 'image/jpeg')
    }
    # No Content-Range / X-Upload-Id headers — this is always the
    # single-shot upload path, never the resumable one.
    assert 'json' not in mock_request.call_args.kwargs


def test_delete_asset_returns_none_on_204() -> None:
    client = PlayerAPIClient(_make_player())

    with patch.object(
        AnthiasSession,
        'request',
        return_value=_response(status_code=204, content=b''),
    ) as mock_request:
        client.delete_asset('abc123')

    method, url = mock_request.call_args.args[:2]
    assert method == 'DELETE'
    assert url.endswith('/api/v2/assets/abc123')


def test_reorder_assets_sends_comma_joined_ids() -> None:
    client = PlayerAPIClient(_make_player())

    with patch.object(
        AnthiasSession,
        'request',
        return_value=_response(status_code=204, content=b''),
    ) as mock_request:
        client.reorder_assets(['a', 'b', 'c'])

    method, url = mock_request.call_args.args[:2]
    assert method == 'POST'
    assert url.endswith('/api/v2/assets/order')
    assert mock_request.call_args.kwargs['json'] == {'ids': 'a,b,c'}


def test_control_uses_get_not_post() -> None:
    # AssetsControlViewMixin is a GET on the player side — a client
    # sending POST here would 405 against a real player.
    client = PlayerAPIClient(_make_player())

    with patch.object(
        AnthiasSession,
        'request',
        return_value=_response(json_data='Asset switched'),
    ) as mock_request:
        client.control('next')

    method, url = mock_request.call_args.args[:2]
    assert method == 'GET'
    assert url.endswith('/api/v2/assets/control/next')


def test_skip_ssl_verify_disables_certificate_verification() -> None:
    client = PlayerAPIClient(_make_player(skip_ssl_verify=True))

    with patch.object(
        AnthiasSession, 'request', return_value=_response(json_data={})
    ) as mock_request:
        client.get_info()

    assert mock_request.call_args.kwargs['verify'] is False


@pytest.mark.parametrize(
    'method_name,args',
    [
        ('reboot', ()),
        ('shutdown', ()),
    ],
)
def test_device_control_endpoints(
    method_name: str, args: tuple[Any, ...]
) -> None:
    client = PlayerAPIClient(_make_player())

    with patch.object(
        AnthiasSession,
        'request',
        return_value=_response(status_code=200, json_data=None),
    ) as mock_request:
        getattr(client, method_name)(*args)

    method, url = mock_request.call_args.args[:2]
    assert method == 'POST'
    assert url.endswith(f'/api/v2/{method_name}')


def test_set_display_power_posts_to_state_path() -> None:
    client = PlayerAPIClient(_make_player())

    with patch.object(
        AnthiasSession,
        'request',
        return_value=_response(status_code=200, json_data=None),
    ) as mock_request:
        client.set_display_power('on')

    method, url = mock_request.call_args.args[:2]
    assert method == 'POST'
    assert url.endswith('/api/v2/display/on')
