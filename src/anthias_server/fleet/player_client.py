"""Outbound HTTP client the fleet server uses to call one player's
existing v2 REST API.

This is the concrete implementation of the "thin orchestrator" design:
players keep their own local server/DB/API unmodified, and the fleet
server drives them purely by calling that same API remotely — using
the Phase-0 bearer token (``lib.auth.AnthiasAPITokenAuthentication``)
instead of the operator's own login. Methods mirror
``api/urls/v2.py`` 1:1; see that file for the exact request/response
shapes being replicated here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import requests

from anthias_common.http import AnthiasSession

if TYPE_CHECKING:
    from anthias_server.fleet.models import Player

# Short, fixed timeouts so one unreachable player can never hang a
# heartbeat sweep or a UI drill-down waiting on it — (connect, read).
_TIMEOUT_S = (3, 5)


class PlayerAPIError(Exception):
    """The player responded, but with an error status (4xx/5xx)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f'{status_code}: {message}')
        self.status_code = status_code
        self.message = message


class PlayerUnreachableError(Exception):
    """The player could not be reached at all (network/timeout/DNS)."""


class PlayerAPIClient:
    def __init__(self, player: Player) -> None:
        self._base_url = player.base_url.rstrip('/')
        self._api_token = player.get_api_token()
        self._verify_ssl = not player.skip_ssl_verify

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = AnthiasSession()
        session.headers['Authorization'] = f'Bearer {self._api_token}'
        url = f'{self._base_url}/api/v2/{path.lstrip("/")}'
        try:
            response = session.request(
                method,
                url,
                timeout=_TIMEOUT_S,
                verify=self._verify_ssl,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise PlayerUnreachableError(str(exc)) from exc

        if not response.ok:
            raise PlayerAPIError(response.status_code, response.text)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- health / identity ----------------------------------------------

    def get_info(self) -> dict[str, Any]:
        return cast('dict[str, Any]', self._request('GET', 'info'))

    # --- device settings -----------------------------------------------
    # Only meaningful for a fleet-paired player — DeviceSettingsViewV2
    # .patch rejects any other caller's writes once a player is
    # fleet-managed (the token backing this client must be that
    # player's own paired token, or the PATCH 403s).

    def get_device_settings(self) -> dict[str, Any]:
        return cast('dict[str, Any]', self._request('GET', 'device_settings'))

    def update_device_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        return cast(
            'dict[str, Any]',
            self._request('PATCH', 'device_settings', json=data),
        )

    # --- assets -----------------------------------------------------------

    def list_assets(self) -> list[dict[str, Any]]:
        return cast('list[dict[str, Any]]', self._request('GET', 'assets'))

    def create_asset(self, data: dict[str, Any]) -> dict[str, Any]:
        return cast(
            'dict[str, Any]', self._request('POST', 'assets', json=data)
        )

    def upload_file(
        self, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """POST a single-shot multipart upload to the player's
        ``v2/file_asset`` endpoint. Mirrors ``FileAssetViewMixin.post``'s
        non-resumable path — no ``Content-Range``/``X-Upload-Id``, since
        that machinery exists for the browser's own chunked uploader
        recovering from a flaky LAN connection, not needed for one
        fleet-to-player call. Returns the player's response verbatim
        (``{'uri', 'ext', 'upload_id'}``); ``uri``/``ext`` feed straight
        into a following ``create_asset()`` call the same way the
        player's own upload pipeline consumes them internally.
        """
        return cast(
            'dict[str, Any]',
            self._request(
                'POST',
                'file_asset',
                files={'file_upload': (filename, content, content_type)},
            ),
        )

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return cast(
            'dict[str, Any]', self._request('GET', f'assets/{asset_id}')
        )

    def update_asset(
        self, asset_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return cast(
            'dict[str, Any]',
            self._request('PATCH', f'assets/{asset_id}', json=data),
        )

    def delete_asset(self, asset_id: str) -> None:
        self._request('DELETE', f'assets/{asset_id}')

    def reorder_assets(self, asset_ids: list[str]) -> None:
        self._request(
            'POST', 'assets/order', json={'ids': ','.join(asset_ids)}
        )

    def control(self, command: str) -> None:
        # Mirrors AssetsControlViewMixin — deliberately a GET, not a
        # POST, matching the player's own v2/assets/control/<command>.
        self._request('GET', f'assets/control/{command}')

    # --- device control -----------------------------------------------

    def reboot(self) -> None:
        self._request('POST', 'reboot')

    def shutdown(self) -> None:
        self._request('POST', 'shutdown')

    def set_display_power(self, state: str) -> None:
        self._request('POST', f'display/{state}')
