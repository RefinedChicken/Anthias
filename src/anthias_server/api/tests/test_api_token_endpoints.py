"""Tests for the REST API-token endpoints (``/api/v2/auth/tokens``).

Complements ``tests/test_api_tokens_view.py`` (the HTML-form surface)
and the bearer-token-authentication tests in ``tests/test_auth.py`` —
this file only covers the new REST management surface: listing,
creating, and revoking ``AnthiasAPIToken`` rows over the API itself.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from anthias_server.api.models import AnthiasAPIToken
from anthias_server.lib.auth import hash_api_token, issue_api_token

_PWD_OPERATOR = 'fixture-token-api-pwd'  # NOSONAR


def _make_operator(username: str = 'alice') -> User:
    return User.objects.create_superuser(
        username=username,
        password=_PWD_OPERATOR,  # NOSONAR
    )


def _list_url() -> str:
    return reverse('api:api_token_list_v2')


def _detail_url(token_id: int) -> str:
    return reverse('api:api_token_detail_v2', args=[token_id])


@pytest.mark.django_db
def test_list_tokens_returns_only_operators_own_tokens() -> None:
    operator = _make_operator()
    other = _make_operator(username='mallory')
    issue_api_token(operator, 'mine')
    issue_api_token(other, 'not-mine')

    client = Client()
    client.force_login(operator)
    response = client.get(_list_url())

    assert response.status_code == 200
    names = {row['name'] for row in response.json()}
    assert names == {'mine'}


@pytest.mark.django_db
def test_list_tokens_never_includes_the_hash() -> None:
    operator = _make_operator()
    issue_api_token(operator, 'fleet-server')

    client = Client()
    client.force_login(operator)
    response = client.get(_list_url())

    assert response.status_code == 200
    row = response.json()[0]
    assert 'token_hash' not in row
    assert 'token' not in row
    assert row['prefix']


@pytest.mark.django_db
def test_list_tokens_without_operator_account_is_empty() -> None:
    client = Client()
    response = client.get(_list_url())

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.django_db
def test_create_token_persists_row_and_returns_raw_value_once() -> None:
    operator = _make_operator()
    client = Client()
    client.force_login(operator)

    response = client.post(
        _list_url(), {'name': 'fleet-server'}, content_type='application/json'
    )

    assert response.status_code == 201
    body = response.json()
    assert body['name'] == 'fleet-server'
    assert body['token'].startswith('ant_')

    token = AnthiasAPIToken.objects.get(user=operator, name='fleet-server')
    assert hash_api_token(body['token']) == token.token_hash


@pytest.mark.django_db
def test_create_token_accepts_expires_at() -> None:
    operator = _make_operator()
    client = Client()
    client.force_login(operator)

    response = client.post(
        _list_url(),
        {'name': 'temp', 'expires_at': '2030-01-01T00:00:00Z'},
        content_type='application/json',
    )

    assert response.status_code == 201
    token = AnthiasAPIToken.objects.get(user=operator, name='temp')
    assert token.expires_at is not None


@pytest.mark.django_db
def test_create_token_rejects_blank_name() -> None:
    operator = _make_operator()
    client = Client()
    client.force_login(operator)

    response = client.post(
        _list_url(), {'name': ''}, content_type='application/json'
    )

    assert response.status_code == 400
    assert not AnthiasAPIToken.objects.filter(user=operator).exists()


@pytest.mark.django_db
def test_create_token_without_operator_account_is_rejected() -> None:
    client = Client()
    response = client.post(
        _list_url(), {'name': 'fleet-server'}, content_type='application/json'
    )

    assert response.status_code == 400
    assert AnthiasAPIToken.objects.count() == 0


@pytest.mark.django_db
def test_create_token_via_bearer_token_is_forbidden() -> None:
    """A caller authenticated with a bearer token must not be able to
    mint further tokens with that same credential — see
    ``_reject_bearer_token_auth``."""
    operator = _make_operator()
    _, raw_token = issue_api_token(operator, 'existing')

    client = Client()
    response = client.post(
        _list_url(),
        {'name': 'escalated'},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {raw_token}',
    )

    assert response.status_code == 403
    assert not AnthiasAPIToken.objects.filter(
        user=operator, name='escalated'
    ).exists()


@pytest.mark.django_db
def test_revoke_token_deletes_only_the_owning_users_token() -> None:
    operator = _make_operator()
    other = _make_operator(username='mallory')
    owned_token, _ = issue_api_token(operator, 'mine')
    other_token, _ = issue_api_token(other, 'not-mine')

    client = Client()
    client.force_login(operator)

    response = client.delete(_detail_url(other_token.pk))
    assert response.status_code == 404
    assert AnthiasAPIToken.objects.filter(pk=other_token.pk).exists()

    response = client.delete(_detail_url(owned_token.pk))
    assert response.status_code == 204
    assert not AnthiasAPIToken.objects.filter(pk=owned_token.pk).exists()


@pytest.mark.django_db
def test_revoke_unknown_token_returns_404() -> None:
    operator = _make_operator()
    client = Client()
    client.force_login(operator)

    response = client.delete(_detail_url(999999))

    assert response.status_code == 404


@pytest.mark.django_db
def test_revoke_token_via_bearer_token_is_forbidden() -> None:
    operator = _make_operator()
    owned_token, raw_token = issue_api_token(operator, 'mine')

    client = Client()
    response = client.delete(
        _detail_url(owned_token.pk),
        HTTP_AUTHORIZATION=f'Bearer {raw_token}',
    )

    assert response.status_code == 403
    assert AnthiasAPIToken.objects.filter(pk=owned_token.pk).exists()
