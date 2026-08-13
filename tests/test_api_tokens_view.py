"""Tests for the settings-page API-token views (create/revoke).

Deliberately checks the redirect + DB state rather than following the
redirect into a full ``/settings/`` render — that page also calls
``page_context.device_settings()``, which reads ``/proc/cpuinfo`` via
``anthias_common.device_helper.parse_cpu_info`` and only exists on
Linux hosts, not this suite's concern. Rendering-with-tokens coverage
lives in the Docker-based / CI runs of ``tests/test_template_views.py``.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from anthias_server.api.models import AnthiasAPIToken
from anthias_server.lib.auth import hash_api_token

_PWD_OPERATOR = 'fixture-token-view-pwd'  # NOSONAR


def _make_operator() -> User:
    user = User.objects.create_user(
        username='alice', password=_PWD_OPERATOR, is_staff=True
    )
    return user


@pytest.mark.django_db
def test_api_tokens_create_persists_row_and_stashes_raw_in_session() -> None:
    operator = _make_operator()
    client = Client()
    client.force_login(operator)

    response = client.post(
        reverse('anthias_app:api_tokens_create'), {'name': 'fleet-server'}
    )

    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:settings')

    token = AnthiasAPIToken.objects.get(user=operator, name='fleet-server')
    assert token.token_hash

    stashed = client.session['new_api_token']
    assert stashed['name'] == 'fleet-server'
    assert hash_api_token(stashed['raw']) == token.token_hash


@pytest.mark.django_db
def test_api_tokens_create_rejects_blank_name() -> None:
    operator = _make_operator()
    client = Client()
    client.force_login(operator)

    client.post(reverse('anthias_app:api_tokens_create'), {'name': '  '})

    assert not AnthiasAPIToken.objects.filter(user=operator).exists()


@pytest.mark.django_db
def test_api_tokens_create_without_operator_account_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No persisted operator row exists yet — a token can't be scoped
    to a nonexistent User, so creation must be a no-op rather than a
    500 from the non-nullable FK."""
    monkeypatch.setattr(
        'anthias_server.lib.auth._persisted_operator', lambda: None
    )
    # settings_view/api_tokens_create both run behind @authorized, which
    # passes through unconditionally while auth_backend is '' (the test
    # default) — no session/login needed to reach the view itself.
    client = Client()

    client.post(
        reverse('anthias_app:api_tokens_create'), {'name': 'fleet-server'}
    )

    assert AnthiasAPIToken.objects.count() == 0


@pytest.mark.django_db
def test_api_tokens_revoke_deletes_only_the_owning_users_token() -> None:
    operator = _make_operator()
    other_user = User.objects.create_user(
        username='mallory', password=_PWD_OPERATOR
    )
    from anthias_server.lib.auth import issue_api_token

    owned_token, _ = issue_api_token(operator, 'fleet-server')
    other_token, _ = issue_api_token(other_user, 'not-mine')

    client = Client()
    client.force_login(operator)

    # Can't revoke a token owned by a different user.
    client.post(
        reverse('anthias_app:api_tokens_revoke', args=[other_token.pk])
    )
    assert AnthiasAPIToken.objects.filter(pk=other_token.pk).exists()

    # Can revoke its own token.
    client.post(
        reverse('anthias_app:api_tokens_revoke', args=[owned_token.pk])
    )
    assert not AnthiasAPIToken.objects.filter(pk=owned_token.pk).exists()


@pytest.mark.django_db
def test_new_api_token_shown_once_then_popped_from_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The freshly-created token must render on the very next
    ``/settings/`` GET, then never again — session.pop() (not a plain
    read) is what makes a page refresh or a second tab safe."""
    # /proc/cpuinfo only exists on Linux; stub the one Linux-specific
    # call settings_view's context builder makes so the real view +
    # middleware + template stack can run on any host.
    monkeypatch.setattr(
        'anthias_server.app.page_context.device_helper.parse_cpu_info',
        lambda: {'cpu_count': 0},
    )

    operator = _make_operator()
    client = Client()
    client.force_login(operator)
    client.post(
        reverse('anthias_app:api_tokens_create'), {'name': 'fleet-server'}
    )
    raw_token = client.session['new_api_token']['raw']

    first = client.get(reverse('anthias_app:settings'))
    assert first.status_code == 200
    assert raw_token in first.content.decode()
    assert 'new_api_token' not in client.session

    second = client.get(reverse('anthias_app:settings'))
    assert second.status_code == 200
    assert raw_token not in second.content.decode()
