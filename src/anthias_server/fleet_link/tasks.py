"""Celery-beat-driven pairing poll (plan §8, §9) — reuses the existing
Celery task-queue infrastructure rather than adding a new one, per the
plan's "don't introduce unnecessary tech" instruction. Registered from
``anthias_server.celery_tasks.setup_periodic_tasks`` (a deferred
import there, to avoid a circular import at module-load time — this
module imports the ``celery`` app instance from that same file). Kept
in this app rather than in ``celery_tasks.py`` itself so fleet_link
concerns stay in the fleet_link app, matching where its models/admin
already live.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.utils import timezone

from anthias_server.celery_tasks import celery
from anthias_server.fleet_link.models import FleetPairing, PlayerIdentity

logger = logging.getLogger(__name__)

# How often the beat schedule ticks this task (registered from
# celery_tasks.setup_periodic_tasks). Only meaningful while a pairing
# is 'pending' — the task is a cheap single-query no-op otherwise, so
# a short interval doesn't cost much even across a large fleet of
# mostly-standalone devices.
POLL_INTERVAL_S = 10

_POLL_CONNECT_TIMEOUT_S = 10
_POLL_READ_TIMEOUT_S = 20
_ACK_CONNECT_TIMEOUT_S = 10
_ACK_READ_TIMEOUT_S = 20


@celery.task(
    soft_time_limit=_POLL_CONNECT_TIMEOUT_S + _POLL_READ_TIMEOUT_S + 10,
    time_limit=_POLL_CONNECT_TIMEOUT_S + _POLL_READ_TIMEOUT_S + 20,
)
def poll_fleet_pairing() -> None:
    """One tick of the pairing handshake (plan §8, steps 3-6).

    A no-op whenever there is no ``pending`` ``FleetPairing`` row —
    once a pairing is ``active``, continued polling/heartbeat is the
    desired-state sync engine's job (a later phase), not this task's.
    See ``FleetPairing``'s class docstring on why authority fields
    stay ``local`` through this phase regardless of pairing state.
    """
    pairing = FleetPairing.current()
    if pairing is None or pairing.status != FleetPairing.PENDING:
        return

    now = timezone.now()
    if (
        pairing.pairing_code_expires_at is not None
        and pairing.pairing_code_expires_at <= now
    ):
        # Interrupted-pairing recovery (plan §8 step 6): the Player
        # never completed the handshake before the code's TTL
        # expired. Delete the row outright — no partial state left
        # behind, the device reverts to standalone.
        logger.info(
            'Pairing code %s expired before approval; abandoning.',
            pairing.pairing_user_code,
        )
        pairing.delete()
        return

    try:
        response = requests.post(
            f'{pairing.fleet_base_url.rstrip("/")}/api/pairing/poll',
            json={
                'device_code': pairing.pairing_device_code,
                'user_code': pairing.pairing_user_code,
                'device_id': str(PlayerIdentity.get_or_create().device_id),
            },
            timeout=(_POLL_CONNECT_TIMEOUT_S, _POLL_READ_TIMEOUT_S),
        )
    except requests.RequestException as exc:
        # Transient — Fleet unreachable, DNS hiccup, etc. Same offline
        # posture the sync engine will use (plan §9/§14): retry on the
        # next tick, no state change here.
        logger.warning(
            'Pairing poll against %s failed: %s', pairing.fleet_base_url, exc
        )
        return

    if response.status_code == 429:
        # Fleet's own per-device-code rate gate; next tick retries.
        return
    if response.status_code == 409:
        logger.warning(
            'Pairing code %s collided with another pending request; '
            'abandoning — start pairing again for a fresh code.',
            pairing.pairing_user_code,
        )
        pairing.delete()
        return
    if response.status_code >= 400:
        logger.warning(
            'Pairing poll against %s returned HTTP %s; retrying next tick.',
            pairing.fleet_base_url,
            response.status_code,
        )
        return

    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            'Pairing poll against %s returned a non-JSON body; '
            'retrying next tick.',
            pairing.fleet_base_url,
        )
        return

    fleet_status = payload.get('status')
    if fleet_status in ('pending', 'completed'):
        return
    if fleet_status in ('expired', 'code_collision'):
        logger.info(
            'Fleet reports pairing code %s as %s; abandoning.',
            pairing.pairing_user_code,
            fleet_status,
        )
        pairing.delete()
        return
    if fleet_status != 'approved':
        logger.warning(
            'Pairing poll against %s returned unexpected status %r; '
            'retrying next tick.',
            pairing.fleet_base_url,
            fleet_status,
        )
        return

    _complete_pairing(pairing, payload)


def _complete_pairing(pairing: FleetPairing, payload: dict[str, Any]) -> None:
    """Handle a Fleet ``approved`` response: acknowledge receipt, then
    store the credential and flip to ``active``.

    Acknowledging first is what stops Fleet from reissuing further
    credentials for this request (see the Fleet-side
    ``PairingRequest`` docstring's "reissue until ack" note) — if the
    ack call itself fails, nothing here is persisted, so the *next*
    poll tick simply repeats this from scratch against a freshly
    reissued (and this one now-revoked) credential rather than leaving
    an inconsistent half-paired row.
    """
    raw_credential = payload.get('credential')
    if not raw_credential:
        logger.warning(
            'Fleet approved pairing code %s but sent no credential; '
            'retrying next tick.',
            pairing.pairing_user_code,
        )
        return

    try:
        ack_response = requests.post(
            f'{pairing.fleet_base_url.rstrip("/")}/api/pairing/ack',
            headers={'Authorization': f'Bearer {raw_credential}'},
            timeout=(_ACK_CONNECT_TIMEOUT_S, _ACK_READ_TIMEOUT_S),
        )
        ack_response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            'Pairing ack against %s failed: %s; retrying next tick '
            '(Fleet will reissue a fresh credential).',
            pairing.fleet_base_url,
            exc,
        )
        return

    pairing.device_credential = raw_credential
    pairing.status = FleetPairing.ACTIVE
    pairing.paired_at = timezone.now()
    pairing.pairing_user_code = None
    pairing.pairing_device_code = None
    pairing.pairing_code_expires_at = None
    pairing.save(
        update_fields=[
            'device_credential',
            'status',
            'paired_at',
            'pairing_user_code',
            'pairing_device_code',
            'pairing_code_expires_at',
            'updated_at',
        ]
    )
    logger.info(
        'Pairing complete; device is now managed by %s.',
        pairing.fleet_base_url,
    )
