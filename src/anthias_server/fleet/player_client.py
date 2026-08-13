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

    # --- assets -----------------------------------------------------------

    def list_assets(self) -> list[dict[str, Any]]:
        return cast('list[dict[str, Any]]', self._request('GET', 'assets'))

    def create_asset(self, data: dict[str, Any]) -> dict[str, Any]:
        return cast(
            'dict[str, Any]', self._request('POST', 'assets', json=data)
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
