"""Tests for the fleet server's heartbeat Celery tasks
(``anthias_server.fleet.tasks``)."""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from anthias_server.fleet.models import (
    AssetPushJob,
    AssetPushJobTarget,
    Player,
    PlayerGroup,
)
from anthias_server.fleet.player_client import (
    PlayerAPIError,
    PlayerUnreachableError,
)
from anthias_server.fleet.tasks import (
    heartbeat_sweep,
    poll_player_heartbeat,
    push_asset_to_group,
    push_asset_to_player,
)

_RAW_TOKEN = 'ant_fixture-task-token'  # NOSONAR


def _make_player(**overrides: Any) -> Player:
    player = Player(
        name=overrides.pop('name', 'Lobby TV'),
        base_url=overrides.pop('base_url', 'http://192.168.1.50:8080'),
    )
    player.set_api_token(_RAW_TOKEN)
    player.save()
    return player


@pytest.mark.django_db
def test_poll_player_heartbeat_updates_fields_on_success() -> None:
    player = _make_player()
    info = {
        'anthias_version': 'v2026.8.0',
        'mac_address': 'aa:bb:cc:dd:ee:ff',
        'device_model': 'Raspberry Pi 4',
        'loadavg': 0.42,
        'free_space': '10.0 GB',
        'display_power': 'on',
        'up_to_date': True,
        'uptime': {'days': 3, 'hours': 5.5},
        'memory': {'total': 2048, 'used': 512, 'low_ram': False},
    }

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.return_value = info
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.is_reachable is True
    assert player.mac_address == 'aa:bb:cc:dd:ee:ff'
    assert player.device_model == 'Raspberry Pi 4'
    assert player.anthias_version == 'v2026.8.0'
    assert player.loadavg_15min == 0.42
    assert player.free_space == '10.0 GB'
    assert player.display_power == 'on'
    assert player.up_to_date is True
    assert player.uptime_days == 3
    assert player.uptime_hours == 5.5
    assert player.memory == {'total': 2048, 'used': 512, 'low_ram': False}
    assert player.last_reachability_check is not None
    assert player.last_error == ''


@pytest.mark.django_db
def test_poll_player_heartbeat_keeps_telemetry_on_missing_keys() -> None:
    # A version-skewed player's /api/v2/info response missing a key
    # must not null out previously-good telemetry.
    player = _make_player()
    player.loadavg_15min = 1.5
    player.free_space = '5.0 GB'
    player.display_power = 'on'
    player.up_to_date = True
    player.uptime_days = 1
    player.uptime_hours = 2.0
    player.memory = {'total': 1024}
    player.save()

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.return_value = {
            'mac_address': 'aa:bb:cc:dd:ee:ff',
        }
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.loadavg_15min == 1.5
    assert player.free_space == '5.0 GB'
    assert player.display_power == 'on'
    assert player.up_to_date is True
    assert player.uptime_days == 1
    assert player.uptime_hours == 2.0
    assert player.memory == {'total': 1024}


@pytest.mark.django_db
def test_poll_player_heartbeat_marks_unreachable_on_network_failure() -> None:
    player = _make_player()

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.side_effect = (
            PlayerUnreachableError('connection refused')
        )
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.is_reachable is False
    assert player.last_reachability_check is not None
    assert 'connection refused' in player.last_error


@pytest.mark.django_db
def test_poll_player_heartbeat_marks_unreachable_on_api_error() -> None:
    player = _make_player()

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_info.side_effect = PlayerAPIError(
            503, 'Service Unavailable'
        )
        poll_player_heartbeat(player.id)

    player.refresh_from_db()
    assert player.is_reachable is False
    assert player.last_reachability_check is not None
    assert '503' in player.last_error


@pytest.mark.django_db
def test_poll_player_heartbeat_noops_for_deleted_player() -> None:
    # No Player row with this id exists — must not raise.
    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        poll_player_heartbeat(999999)

    mock_client_cls.assert_not_called()


@pytest.mark.django_db
def test_heartbeat_sweep_fans_out_one_delay_call_per_player() -> None:
    player_a = _make_player(name='A')
    player_b = _make_player(name='B')

    with patch(
        'anthias_server.fleet.tasks.poll_player_heartbeat.delay'
    ) as mock_delay:
        heartbeat_sweep()

    assert mock_delay.call_count == 2
    called_ids = {call.args[0] for call in mock_delay.call_args_list}
    assert called_ids == {player_a.id, player_b.id}


@pytest.mark.django_db
def test_heartbeat_sweep_noops_with_no_players() -> None:
    with patch(
        'anthias_server.fleet.tasks.poll_player_heartbeat.delay'
    ) as mock_delay:
        heartbeat_sweep()

    mock_delay.assert_not_called()


# ---------------------------------------------------------------------------
# Bulk asset push


def _make_job(**overrides: Any) -> AssetPushJob:
    group = overrides.pop('group', None) or PlayerGroup.objects.create(
        name='Lobby'
    )
    source_player = overrides.pop('source_player', None) or _make_player(
        name='Source'
    )
    return AssetPushJob.objects.create(
        group=group,
        source_player=source_player,
        source_asset_id=overrides.pop('source_asset_id', 'asset-1'),
        **overrides,
    )


@pytest.fixture
def fleet_push_staging_dir(settings: Any, tmp_path: Path) -> Path:
    staging_dir = tmp_path / 'push-staging'
    settings.FLEET_PUSH_STAGING_DIR = str(staging_dir)
    return staging_dir


@pytest.mark.django_db
def test_push_asset_to_group_stages_file_and_fans_out(
    fleet_push_staging_dir: Path,
) -> None:
    job = _make_job()
    target_a = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )
    target_b = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='B')
    )
    raw_bytes = b'\xff\xd8\xff'

    with (
        patch('anthias_server.fleet.tasks.PlayerAPIClient') as mock_client_cls,
        patch(
            'anthias_server.fleet.tasks.push_asset_to_player.delay'
        ) as mock_delay,
    ):
        mock_client_cls.return_value.get_asset.return_value = {
            'name': 'Lobby poster.jpg',
            'mimetype': 'image',
            'duration': 15,
        }
        mock_client_cls.return_value.get_asset_content.return_value = {
            'type': 'file',
            'filename': 'Lobby poster.jpg',
            'content': b64encode(raw_bytes).decode(),
            'mimetype': 'image/jpeg',
        }
        push_asset_to_group(job.id)

    job.refresh_from_db()
    assert job.mimetype == 'image'
    assert job.duration == 15
    assert job.asset_name == 'Lobby poster.jpg'
    assert job.staged_content_path
    assert Path(job.staged_content_path).read_bytes() == raw_bytes

    assert mock_delay.call_count == 2
    called_ids = {call.args[0] for call in mock_delay.call_args_list}
    assert called_ids == {target_a.id, target_b.id}


@pytest.mark.django_db
def test_push_asset_to_group_webpage_asset_skips_staging() -> None:
    job = _make_job()
    AssetPushJobTarget.objects.create(job=job, player=_make_player(name='A'))

    with (
        patch('anthias_server.fleet.tasks.PlayerAPIClient') as mock_client_cls,
        patch('anthias_server.fleet.tasks.push_asset_to_player.delay'),
    ):
        mock_client_cls.return_value.get_asset.return_value = {
            'name': 'Menu board',
            'mimetype': 'webpage',
            'duration': 10,
            'uri': 'https://example.com/menu',
        }
        push_asset_to_group(job.id)

    job.refresh_from_db()
    assert job.staged_content_path == ''
    assert job.source_uri == 'https://example.com/menu'
    mock_client_cls.return_value.get_asset_content.assert_not_called()


@pytest.mark.django_db
def test_push_asset_to_group_url_backed_image_skips_staging() -> None:
    # An image/video asset can be backed by a remote URL rather than an
    # uploaded file — get_asset_content()'s {'type': 'url'} response
    # distinguishes this from a real upload, even though mimetype is
    # 'image'/'video' either way.
    job = _make_job()
    AssetPushJobTarget.objects.create(job=job, player=_make_player(name='A'))

    with (
        patch('anthias_server.fleet.tasks.PlayerAPIClient') as mock_client_cls,
        patch('anthias_server.fleet.tasks.push_asset_to_player.delay'),
    ):
        mock_client_cls.return_value.get_asset.return_value = {
            'name': 'Remote sign',
            'mimetype': 'image',
            'duration': 10,
        }
        mock_client_cls.return_value.get_asset_content.return_value = {
            'type': 'url',
            'url': 'https://example.com/sign.png',
        }
        push_asset_to_group(job.id)

    job.refresh_from_db()
    assert job.staged_content_path == ''
    assert job.source_uri == 'https://example.com/sign.png'


@pytest.mark.django_db
def test_push_asset_to_group_fails_all_targets_when_source_unreachable() -> (
    None
):
    job = _make_job()
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.get_asset.side_effect = (
            PlayerUnreachableError('connection refused')
        )
        push_asset_to_group(job.id)

    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_FAILED
    assert 'connection refused' in target.error
    assert target.completed_at is not None


@pytest.mark.django_db
def test_push_asset_to_group_fails_all_targets_when_source_player_gone() -> (
    None
):
    job = _make_job()
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )
    job.source_player = None
    job.save()

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        push_asset_to_group(job.id)

    mock_client_cls.assert_not_called()
    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_FAILED


@pytest.mark.django_db
def test_push_asset_to_group_noops_for_deleted_job() -> None:
    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        push_asset_to_group(999999)

    mock_client_cls.assert_not_called()


@pytest.mark.django_db
def test_push_asset_to_player_uploads_staged_file_and_creates_asset(
    fleet_push_staging_dir: Path,
) -> None:
    fleet_push_staging_dir.mkdir(parents=True)
    staged = fleet_push_staging_dir / '1.bin'
    staged.write_bytes(b'\xff\xd8\xff')

    job = _make_job(
        asset_name='Lobby poster.jpg',
        mimetype='image',
        duration=15,
        staged_content_path=str(staged),
    )
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.list_assets.return_value = []
        mock_client_cls.return_value.upload_file.return_value = {
            'uri': '/data/.anthias/assets/xyz.tmp',
            'ext': '.jpg',
        }
        mock_client_cls.return_value.create_asset.return_value = {}
        push_asset_to_player(target.id)

    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_SUCCESS
    assert target.completed_at is not None

    mock_client_cls.return_value.upload_file.assert_called_once()
    filename, content, content_type = (
        mock_client_cls.return_value.upload_file.call_args.args
    )
    assert filename == 'Lobby poster.jpg'
    assert content == b'\xff\xd8\xff'
    assert content_type == 'image/jpeg'

    sent = mock_client_cls.return_value.create_asset.call_args.args[0]
    assert sent['uri'] == '/data/.anthias/assets/xyz.tmp'
    assert sent['ext'] == '.jpg'
    assert sent['name'] == 'Lobby poster.jpg'
    assert sent['mimetype'] == 'image'
    assert sent['duration'] == 15

    # Only target — the staged file must be cleaned up once done.
    assert not staged.exists()


@pytest.mark.django_db
def test_push_asset_to_player_url_type_creates_asset_directly() -> None:
    job = _make_job(
        asset_name='Menu board',
        mimetype='webpage',
        duration=10,
        source_uri='https://example.com/menu',
    )
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.list_assets.return_value = []
        mock_client_cls.return_value.create_asset.return_value = {}
        push_asset_to_player(target.id)

    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_SUCCESS
    mock_client_cls.return_value.upload_file.assert_not_called()
    sent = mock_client_cls.return_value.create_asset.call_args.args[0]
    assert sent['uri'] == 'https://example.com/menu'


@pytest.mark.django_db
def test_push_asset_to_player_skips_when_uri_already_present() -> None:
    job = _make_job(
        asset_name='Menu board',
        mimetype='webpage',
        source_uri='https://example.com/menu',
    )
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.list_assets.return_value = [
            {'uri': 'https://example.com/menu', 'name': 'Existing'}
        ]
        push_asset_to_player(target.id)

    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_SKIPPED
    mock_client_cls.return_value.create_asset.assert_not_called()


@pytest.mark.django_db
def test_push_asset_to_player_skips_when_name_already_present(
    fleet_push_staging_dir: Path,
) -> None:
    fleet_push_staging_dir.mkdir(parents=True)
    staged = fleet_push_staging_dir / '1.bin'
    staged.write_bytes(b'\xff\xd8\xff')

    job = _make_job(
        asset_name='Lobby poster.jpg',
        mimetype='image',
        staged_content_path=str(staged),
    )
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.list_assets.return_value = [
            {'uri': 'x', 'name': 'Lobby poster.jpg'}
        ]
        push_asset_to_player(target.id)

    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_SKIPPED
    mock_client_cls.return_value.upload_file.assert_not_called()
    mock_client_cls.return_value.create_asset.assert_not_called()


@pytest.mark.django_db
def test_push_asset_to_player_marks_failed_on_api_error() -> None:
    job = _make_job(
        asset_name='Menu board', mimetype='webpage', source_uri='https://x'
    )
    target = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.list_assets.return_value = []
        mock_client_cls.return_value.create_asset.side_effect = PlayerAPIError(
            503, 'Service Unavailable'
        )
        push_asset_to_player(target.id)

    target.refresh_from_db()
    assert target.status == AssetPushJobTarget.STATUS_FAILED
    assert '503' in target.error


@pytest.mark.django_db
def test_push_asset_to_player_noops_for_deleted_target() -> None:
    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        push_asset_to_player(999999)

    mock_client_cls.assert_not_called()


@pytest.mark.django_db
def test_push_asset_to_player_keeps_staged_file_while_others_pending(
    fleet_push_staging_dir: Path,
) -> None:
    fleet_push_staging_dir.mkdir(parents=True)
    staged = fleet_push_staging_dir / '1.bin'
    staged.write_bytes(b'\xff\xd8\xff')

    job = _make_job(
        asset_name='Lobby poster.jpg',
        mimetype='image',
        staged_content_path=str(staged),
    )
    target_a = AssetPushJobTarget.objects.create(
        job=job, player=_make_player(name='A')
    )
    # A second, still-pending target keeps the staged file alive.
    AssetPushJobTarget.objects.create(job=job, player=_make_player(name='B'))

    with patch(
        'anthias_server.fleet.tasks.PlayerAPIClient'
    ) as mock_client_cls:
        mock_client_cls.return_value.list_assets.return_value = []
        mock_client_cls.return_value.upload_file.return_value = {'uri': 'x'}
        mock_client_cls.return_value.create_asset.return_value = {}
        push_asset_to_player(target_a.id)

    assert staged.exists()
