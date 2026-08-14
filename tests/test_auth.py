"""Tests for anthias_server.lib.auth.

The legacy Auth/NoAuth/BasicAuth class hierarchy has been retired —
auth is now Django's built-in (session via DRF
``SessionAuthentication``, the deprecation-logging
``DeprecatedBasicAuthentication`` for back-compat with pre-2826
headless callers, and ``AnthiasAPITokenAuthentication`` for
unattended integrations). What's covered here:

* The hash helpers (round-trip, legacy-format detection) — still used
  by the data migration to gate which conf rows can be promoted into
  User.password.
* ``generate_api_token`` / ``hash_api_token`` / ``issue_api_token`` —
  token creation, hashing, and storage.
* The ``@authorized`` shim — feature-flagged, must pass through when
  auth is disabled and redirect to /login otherwise.
* The Session, Basic, and Bearer-token paths reaching the JSON API.
* ``apply_auth_settings`` — single source of truth for the v2 API's
  settings-save auth-update flow (backend switching, initial-operator
  creation) — kept intact for that surface only, see its docstring.
* ``apply_self_account_changes`` — the HTML-only self-service
  username/password change for whichever user is logged in.
* ``require_setup_complete`` / ``require_settings_access`` — the
  mandatory first-run gate and the Settings-family permission gate
  added for the multi-user admin-management feature, plus the
  user-CRUD views and the ``/setup/`` wizard view that sit behind them
  in ``anthias_server.app.views``.
"""

from __future__ import annotations

from base64 import b64encode
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from anthias_server.lib import auth
from anthias_server.lib.auth import (
    AuthSettingsError,
    _is_last_admin,
    _is_legacy_sha256,
    apply_auth_settings,
    apply_self_account_changes,
    authorized,
    hash_password,
    operator_username,
    require_settings_access,
    require_setup_complete,
    verify_password,
)

# Centralised fixture credentials so Sonar's S2068 (potentially-hardcoded
# credential) fires on a single suppressed line per value instead of
# once per assertion across the file. These strings never reach a real
# credential store — they're consumed only by the in-memory test User
# rows below.
_PWD_OLD = 'fixture-old-pwd'  # NOSONAR
_PWD_NEW = 'fixture-new-pwd'  # NOSONAR
_PWD_INITIAL = 'fixture-initial-pwd'  # NOSONAR
_PWD_TOKEN_USER = 'fixture-token-pwd'  # NOSONAR
_PWD_WRONG = 'fixture-wrong-pwd'  # NOSONAR
_PWD_THROWAWAY_1 = 'fixture-throwaway-1'  # NOSONAR
_PWD_THROWAWAY_2 = 'fixture-throwaway-2'  # NOSONAR
_PWD_MISMATCH_A = 'fixture-mismatch-a'  # NOSONAR
_PWD_MISMATCH_B = 'fixture-mismatch-b'  # NOSONAR


# ---------------------------------------------------------------------------
# hash_password / verify_password


@pytest.mark.django_db
def test_hash_password_round_trip() -> None:
    hashed = hash_password(_PWD_INITIAL)
    assert hashed != _PWD_INITIAL
    # Django's hashers always produce an algorithm-prefixed string.
    assert '$' in hashed
    assert verify_password(_PWD_INITIAL, hashed) is True
    assert verify_password(_PWD_WRONG, hashed) is False


@pytest.mark.django_db
def test_verify_password_empty_stored_returns_false() -> None:
    assert verify_password('anything', '') is False


@pytest.mark.parametrize(
    'value,expected',
    [
        # 64 hex chars → legacy bare SHA256
        ('a' * 64, True),
        ('0' * 63 + 'f', True),
        ('A' * 64, False),  # uppercase rejected (regex is lowercase only)
        ('a' * 63, False),
        ('a' * 65, False),
        ('pbkdf2_sha256$...', False),
        ('', False),
    ],
)
def test_is_legacy_sha256(value: str, expected: bool) -> None:
    assert _is_legacy_sha256(value) is expected


# ---------------------------------------------------------------------------
# generate_api_token / hash_api_token / issue_api_token


def test_generate_api_token_has_prefix_and_is_unique() -> None:
    token_a = auth.generate_api_token()
    token_b = auth.generate_api_token()
    assert token_a.startswith('ant_')
    assert token_a != token_b


def test_hash_api_token_is_deterministic_and_not_reversible() -> None:
    raw = auth.generate_api_token()
    digest = auth.hash_api_token(raw)
    assert digest == auth.hash_api_token(raw)
    assert digest != raw
    # SHA-256 hex digest.
    assert len(digest) == 64


@pytest.mark.django_db
def test_issue_api_token_creates_row_and_returns_raw_once() -> None:
    from anthias_server.api.models import AnthiasAPIToken

    operator = _make_operator(pwd=_PWD_TOKEN_USER)
    token_row, raw_token = auth.issue_api_token(operator, 'fleet-server')

    assert raw_token.startswith('ant_')
    assert token_row.user_id == operator.pk
    assert token_row.name == 'fleet-server'
    assert token_row.prefix == raw_token[:12]
    assert token_row.token_hash == auth.hash_api_token(raw_token)
    # The raw token itself is never persisted anywhere on the row.
    assert raw_token not in token_row.token_hash
    assert AnthiasAPIToken.objects.filter(pk=token_row.pk).exists()


def test_module_level_linux_user_constant() -> None:
    # Sanity: the constant is read at import and exposed for callers.
    assert isinstance(auth.LINUX_USER, str)
    assert auth.LINUX_USER  # non-empty


# ---------------------------------------------------------------------------
# @authorized — feature flag + redirect contract


def test_authorized_passthrough_when_auth_backend_disabled(
    monkeypatch: Any,
) -> None:
    """settings['auth_backend'] == '' is the NoAuth equivalent — the
    wrapped view runs unconditionally so devices on the default
    un-authenticated config keep working."""
    fake_settings = {'auth_backend': ''}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    assert view(factory.get('/')) == 'ok'


def test_authorized_redirects_when_unauthenticated(monkeypatch: Any) -> None:
    """When auth is enabled and request.user is anonymous, the
    decorator should bounce the caller to /login with ?next= filled in."""
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/system-info/')
    # AnonymousUser is the default when AuthenticationMiddleware hasn't
    # run; emulate that by attaching a MagicMock with is_authenticated=False.
    request.user = MagicMock(is_authenticated=False)
    response = view(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 302
    assert response['Location'].startswith('/login')
    assert 'next=%2Fsystem-info%2F' in response['Location']


def test_authorized_calls_view_when_authenticated(monkeypatch: Any) -> None:
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/')
    request.user = MagicMock(is_authenticated=True)
    assert view(request) == 'ok'


def test_authorized_drops_next_for_unsafe_methods(monkeypatch: Any) -> None:
    """A POST/PUT/PATCH/DELETE that 401s would otherwise produce a
    ?next=/some/write/endpoint that the post-login GET redirect bounces
    back to → 405. Drop next for unsafe methods so the operator lands
    on the dashboard instead."""
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    for method in ('post', 'put', 'patch', 'delete'):
        request = getattr(factory, method)('/api/v2/assets/')
        request.user = MagicMock(is_authenticated=False)
        response = view(request)
        assert isinstance(response, HttpResponse)
        assert response.status_code == 302
        assert response['Location'].endswith('/login/')
        assert 'next=' not in response['Location']


def test_authorized_drops_next_for_htmx_partial(monkeypatch: Any) -> None:
    """Dashboard polls htmx fragments every 5s; if the session expires
    mid-poll we'd otherwise serialize the partial URL into next,
    dumping the operator on a bare table fragment after sign-in."""
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/_partials/asset-table/', HTTP_HX_REQUEST='true')
    request.user = MagicMock(is_authenticated=False)
    response = view(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 302
    assert response['Location'].endswith('/login/')
    assert 'next=' not in response['Location']


def test_authorized_no_args_raises(monkeypatch: Any) -> None:
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view() -> str:
        return 'ok'

    with pytest.raises(ValueError, match='No request object passed'):
        view()


def test_authorized_non_request_arg_raises(monkeypatch: Any) -> None:
    """Calls with positional args that don't include a Request object
    raise — the decorator can't know which side to redirect."""
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(request: Any) -> str:
        return 'ok'

    with pytest.raises(ValueError, match='No request object passed'):
        view('not-a-request')


def test_authorized_finds_request_among_positional_args(
    monkeypatch: Any,
) -> None:
    """A view with extra positional args (e.g. URL captures passed
    positionally, or a DRF method with ``self`` plus a path
    parameter) must still resolve the request — the previous
    ``args[-1]`` heuristic broke for ``def view(self, request,
    asset_id)`` because asset_id ended up where request should be.
    """
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @authorized
    def view(self_: Any, request: Any, asset_id: str) -> str:
        return f'ok:{asset_id}'

    factory = RequestFactory()
    req = factory.get('/foo/')
    req.user = MagicMock(is_authenticated=True)
    # Mimic a DRF bound-method call: (self, request, asset_id).
    assert view(object(), req, 'abc-123') == 'ok:abc-123'


# ---------------------------------------------------------------------------
# operator_username — settings-page lookup


@pytest.mark.django_db
def test_operator_username_empty_when_no_user_exists() -> None:
    assert operator_username() == ''


# ---------------------------------------------------------------------------
# apply_auth_settings — covers the settings-save flow shared by the
# HTML view and DeviceSettingsViewV2.


def _request_with_user(user: Any) -> Any:
    """Build a RequestFactory request and attach the given user (or a
    MagicMock proxy for AnonymousUser)."""
    factory = RequestFactory()
    request = factory.post('/')
    request.user = user
    return request


def _make_operator(username: str = 'alice', pwd: str = _PWD_OLD) -> User:
    """Centralised superuser factory.

    All scattered ``User.objects.create_superuser(..., password=...)``
    call sites in the tests below come through here so Sonar's
    S6437 (hard-coded password) only sees the kwarg in one place,
    suppressed via NOSONAR. The actual password values used in tests
    are still test-only constants defined at module scope above —
    nothing here is a real credential."""
    return User.objects.create_superuser(
        username=username,
        password=pwd,  # NOSONAR
    )


def _make_user(username: str, pwd: str) -> User:
    """Same idea as ``_make_operator`` but for a regular (non-staff)
    user — used only by ``test_operator_username_returns_first_superuser``
    to verify that ``operator_username()`` skips non-superuser rows."""
    return User.objects.create_user(
        username=username,
        password=pwd,  # NOSONAR
    )


@pytest.mark.django_db
def test_operator_username_returns_first_superuser() -> None:
    _make_user(username='non-admin', pwd=_PWD_THROWAWAY_1)
    _make_operator(username='alice', pwd=_PWD_THROWAWAY_2)
    assert operator_username() == 'alice'


@pytest.mark.django_db
def test_apply_auth_settings_initial_enable_creates_superuser() -> None:
    """Auth disabled → enabling for the first time creates a User with
    is_staff/is_superuser=True so the operator can also reach
    /admin/ via Django's admin."""
    request = _request_with_user(MagicMock(is_authenticated=False))
    apply_auth_settings(
        request,
        new_auth_backend='auth_basic',
        current_pwd='',
        new_username='alice',
        new_pwd=_PWD_INITIAL,
        new_pwd_confirm=_PWD_INITIAL,
        prev_auth_backend='',
    )
    user = User.objects.get(username='alice')
    assert user.is_active and user.is_staff and user.is_superuser
    assert user.check_password(_PWD_INITIAL)


@pytest.mark.django_db
def test_apply_auth_settings_initial_enable_requires_username() -> None:
    request = _request_with_user(MagicMock(is_authenticated=False))
    with pytest.raises(AuthSettingsError, match='Must provide username'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd='',
            new_username='',
            new_pwd=_PWD_INITIAL,
            new_pwd_confirm=_PWD_INITIAL,
            prev_auth_backend='',
        )


@pytest.mark.django_db
def test_apply_auth_settings_initial_enable_requires_password() -> None:
    request = _request_with_user(MagicMock(is_authenticated=False))
    with pytest.raises(AuthSettingsError, match='Must provide password'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd='',
            new_username='alice',
            new_pwd='',
            new_pwd_confirm='',
            prev_auth_backend='',
        )


@pytest.mark.django_db
def test_apply_auth_settings_initial_enable_password_mismatch() -> None:
    request = _request_with_user(MagicMock(is_authenticated=False))
    with pytest.raises(AuthSettingsError, match='New passwords do not match'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd='',
            new_username='alice',
            new_pwd=_PWD_MISMATCH_A,
            new_pwd_confirm=_PWD_MISMATCH_B,
            prev_auth_backend='',
        )


@pytest.mark.django_db
def test_apply_auth_settings_change_password_success() -> None:
    user = _make_operator()
    request = _request_with_user(user)
    apply_auth_settings(
        request,
        new_auth_backend='auth_basic',
        current_pwd=_PWD_OLD,
        new_username='alice',
        new_pwd=_PWD_NEW,
        new_pwd_confirm=_PWD_NEW,
        prev_auth_backend='auth_basic',
    )
    user.refresh_from_db()
    assert user.check_password(_PWD_NEW)


@pytest.mark.django_db
def test_apply_auth_settings_change_password_requires_current() -> None:
    user = _make_operator()
    request = _request_with_user(user)
    with pytest.raises(
        AuthSettingsError, match='supply current password to change password'
    ):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd='',
            new_username='alice',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
            prev_auth_backend='auth_basic',
        )


@pytest.mark.django_db
def test_apply_auth_settings_change_password_wrong_current() -> None:
    user = _make_operator()
    request = _request_with_user(user)
    with pytest.raises(AuthSettingsError, match='Incorrect current password'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd=_PWD_WRONG,
            new_username='alice',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
            prev_auth_backend='auth_basic',
        )


@pytest.mark.django_db
def test_apply_auth_settings_change_username_success() -> None:
    user = _make_operator()
    request = _request_with_user(user)
    apply_auth_settings(
        request,
        new_auth_backend='auth_basic',
        current_pwd=_PWD_OLD,
        new_username='bob',
        new_pwd='',
        new_pwd_confirm='',
        prev_auth_backend='auth_basic',
    )
    user.refresh_from_db()
    assert user.username == 'bob'
    # Password is unchanged.
    assert user.check_password(_PWD_OLD)


@pytest.mark.django_db
def test_apply_auth_settings_disable_requires_current_password() -> None:
    user = _make_operator()
    request = _request_with_user(user)
    with pytest.raises(
        AuthSettingsError,
        match='supply current password to change authentication method',
    ):
        apply_auth_settings(
            request,
            new_auth_backend='',
            current_pwd='',
            new_username='',
            new_pwd='',
            new_pwd_confirm='',
            prev_auth_backend='auth_basic',
        )


@pytest.mark.django_db
def test_apply_auth_settings_disable_with_correct_password_succeeds() -> None:
    user = _make_operator()
    request = _request_with_user(user)
    apply_auth_settings(
        request,
        new_auth_backend='',
        current_pwd=_PWD_OLD,
        new_username='',
        new_pwd='',
        new_pwd_confirm='',
        prev_auth_backend='auth_basic',
    )
    # Disabling auth keeps the User row intact so re-enabling later
    # doesn't force a fresh password.
    assert User.objects.filter(username='alice').exists()


@pytest.mark.django_db
def test_apply_auth_settings_rejects_unknown_backend() -> None:
    """A hand-crafted form POST that smuggles an unknown auth_backend
    value (e.g. 'something-else') must be rejected before any DB or
    conf mutation. Otherwise the caller could persist an unknown
    backend and ``@authorized`` would start enforcing login with no
    operator User row to authenticate against → lockout."""
    request = _request_with_user(MagicMock(is_authenticated=False))
    with pytest.raises(
        AuthSettingsError, match='Unknown authentication backend'
    ):
        apply_auth_settings(
            request,
            new_auth_backend='something-else',
            current_pwd='',
            new_username='alice',
            new_pwd=_PWD_INITIAL,
            new_pwd_confirm=_PWD_INITIAL,
            prev_auth_backend='',
        )
    # No User row was created — the validation fired before any
    # mutation.
    assert not User.objects.filter(username='alice').exists()


@pytest.mark.django_db
def test_apply_auth_settings_initial_enable_rejects_short_password() -> None:
    """``AUTH_PASSWORD_VALIDATORS`` is configured with
    MinimumLengthValidator (default 8). Initial enable must reject a
    too-short password instead of silently storing it."""
    request = _request_with_user(MagicMock(is_authenticated=False))
    with pytest.raises(AuthSettingsError, match='too short'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd='',
            new_username='alice',
            new_pwd='short',  # NOSONAR - 5 chars; under MinimumLengthValidator's 8
            new_pwd_confirm='short',  # NOSONAR
            prev_auth_backend='',
        )
    # No half-created User row left behind.
    assert not User.objects.filter(username='alice').exists()


@pytest.mark.django_db
def test_apply_auth_settings_change_password_rejects_too_short() -> None:
    """Same validator stack runs on password change."""
    user = _make_operator()
    request = _request_with_user(user)
    with pytest.raises(AuthSettingsError, match='too short'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd=_PWD_OLD,
            new_username='alice',
            new_pwd='abc',  # NOSONAR - 3 chars
            new_pwd_confirm='abc',  # NOSONAR
            prev_auth_backend='auth_basic',
        )
    # Password unchanged; old still verifies.
    user.refresh_from_db()
    assert user.check_password(_PWD_OLD)


@pytest.mark.django_db
def test_apply_auth_settings_re_enable_with_persisted_user_requires_pwd() -> (
    None
):
    """Privilege-escalation guard: when auth is currently disabled
    (``auth_backend == ''``) but a User row already exists in the DB
    (e.g. preserved by the 0005 migration after enable→disable), an
    UNAUTHENTICATED caller flipping ``auth_backend`` back to
    ``auth_basic`` MUST be challenged for the persisted user's
    current password. Without this gate any LAN attacker could
    re-enable auth with their own credentials and lock the operator
    out."""
    # Pre-existing User row, but no authenticated session — that's
    # the post-disable state.
    _make_operator(username='alice', pwd=_PWD_OLD)
    request = _request_with_user(MagicMock(is_authenticated=False))

    # No current_pwd supplied → must be rejected.
    with pytest.raises(
        AuthSettingsError,
        match='supply current password to change authentication method',
    ):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd='',
            new_username='attacker',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
            prev_auth_backend='',
        )

    # Wrong current_pwd → also rejected.
    with pytest.raises(AuthSettingsError, match='Incorrect current password'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd=_PWD_WRONG,
            new_username='attacker',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
            prev_auth_backend='',
        )

    # Original operator and password are unchanged.
    user = User.objects.get(username='alice')
    assert user.check_password(_PWD_OLD)
    # No attacker User leaked in.
    assert not User.objects.filter(username='attacker').exists()


@pytest.mark.django_db
def test_apply_auth_settings_re_enable_with_correct_pwd_succeeds() -> None:
    """Same setup as the privilege-escalation test, but with the
    correct current password — re-enable should succeed and may
    rotate the operator's username/password as part of the same
    request."""
    _make_operator(username='alice', pwd=_PWD_OLD)
    request = _request_with_user(MagicMock(is_authenticated=False))

    apply_auth_settings(
        request,
        new_auth_backend='auth_basic',
        current_pwd=_PWD_OLD,
        new_username='alice',
        new_pwd=_PWD_NEW,
        new_pwd_confirm=_PWD_NEW,
        prev_auth_backend='',
    )
    user = User.objects.get(username='alice')
    assert user.check_password(_PWD_NEW)


@pytest.mark.django_db
def test_apply_auth_settings_rejects_non_operator_session() -> None:
    """If a recovery superuser was created via
    ``manage.py createsuperuser`` and that recovery account is the
    one currently logged in, ``apply_auth_settings`` must refuse to
    re-key the *operator's* credentials behind their back. The
    canonical operator (first active superuser) is the only account
    that can change auth settings through this flow."""
    # Canonical operator — first active superuser, becomes
    # _persisted_operator().
    operator = _make_operator(username='alice', pwd=_PWD_OLD)
    # Recovery admin from `manage.py createsuperuser`. Also a
    # superuser, but distinct row.
    recovery = _make_operator(username='recovery', pwd=_PWD_NEW)
    request = _request_with_user(recovery)

    with pytest.raises(AuthSettingsError, match='operator account'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd=_PWD_NEW,
            new_username='hijacked',
            new_pwd=_PWD_THROWAWAY_1,
            new_pwd_confirm=_PWD_THROWAWAY_1,
            prev_auth_backend='auth_basic',
        )
    # Operator's credentials are untouched.
    operator.refresh_from_db()
    assert operator.username == 'alice'
    assert operator.check_password(_PWD_OLD)


@pytest.mark.django_db
def test_apply_auth_settings_noop_does_not_write_user_row() -> None:
    """``apply_auth_settings`` runs on every settings POST (the form
    sends the whole page, including unrelated toggles like
    show_splash). When nothing in the auth section actually changes,
    the operator's User row should NOT be re-saved — that's a wasted
    write to ``auth_user`` on every settings save while auth is
    enabled.

    Detect by snapshotting the password hash before the call and
    asserting it's byte-identical afterwards. Django's PBKDF2 hasher
    re-salts on every ``set_password()``, so a stray save() that ran
    set_password again would change the stored hash even with the
    same plaintext. (We can't stamp ``last_login`` since
    ``apply_auth_settings`` doesn't touch it; the hash check is what
    proves we didn't go through the password update branch.)
    """
    operator = _make_operator()
    request = _request_with_user(operator)
    original_hash = operator.password
    original_pk = operator.pk

    # No new username, no new password, no change of backend.
    apply_auth_settings(
        request,
        new_auth_backend='auth_basic',
        current_pwd='',
        new_username='alice',  # same as existing
        new_pwd='',
        new_pwd_confirm='',
        prev_auth_backend='auth_basic',
    )
    operator.refresh_from_db()
    assert operator.pk == original_pk
    assert operator.password == original_hash


@pytest.mark.django_db
def test_apply_auth_settings_change_username_collision() -> None:
    """Renaming the operator to a username that already exists must
    raise a friendly error instead of leaking IntegrityError."""
    operator = _make_operator(username='alice', pwd=_PWD_OLD)
    # Another user (e.g. one created via `manage.py createsuperuser`)
    _make_user(username='bob', pwd=_PWD_THROWAWAY_1)
    request = _request_with_user(operator)
    with pytest.raises(AuthSettingsError, match='already taken'):
        apply_auth_settings(
            request,
            new_auth_backend='auth_basic',
            current_pwd=_PWD_OLD,
            new_username='bob',  # collides
            new_pwd='',
            new_pwd_confirm='',
            prev_auth_backend='auth_basic',
        )
    # Operator's username was NOT changed.
    operator.refresh_from_db()
    assert operator.username == 'alice'


# ---------------------------------------------------------------------------
# DRF authentication paths
#
# Sanity: each surviving credential path reaches the /api/v2/assets
# endpoint when the device has auth enabled. We exercise them through
# the actual HTTP stack rather than mocking out @authorized so we
# catch regressions in the middleware ordering / DRF auth class
# registration.


@pytest.fixture
def authed_operator() -> User:
    return _make_operator(pwd=_PWD_TOKEN_USER)


def _enable_auth() -> Any:
    """Patch the global settings dict so @authorized treats auth as
    enabled. Returns a ``patch.dict`` context manager — use as
    ``with _enable_auth(): ...`` so the patch is reverted on exit."""
    return patch.dict(
        'anthias_server.settings.settings.data', {'auth_backend': 'auth_basic'}
    )


@pytest.mark.django_db
def test_basic_auth_header_authenticates_for_back_compat(
    authed_operator: User,
) -> None:
    """Pre-2826 callers that send Authorization: Basic must keep
    working; we deliberately retained DRF's BasicAuthentication.

    Pin the explicit success contract (200 + JSON list) rather than
    just ``status_code != 302`` — the looser check would still pass
    if BasicAuthentication regressed to returning 401/403/500, which
    would silently break the back-compat headless path.
    """
    creds = b64encode(f'alice:{_PWD_TOKEN_USER}'.encode()).decode('ascii')
    client = Client()
    with _enable_auth():
        response = client.get(
            '/api/v2/assets',
            HTTP_AUTHORIZATION=f'Basic {creds}',
        )
    assert response.status_code == 200
    # Empty asset list — but the type and shape are what we're locking
    # in: a JSON array, not an HTML login page.
    assert response.headers['Content-Type'].startswith('application/json')
    assert response.json() == []


@pytest.mark.django_db
def test_basic_auth_header_rejects_wrong_password(
    authed_operator: User,
) -> None:
    """Wrong Basic-auth credentials must produce a deterministic
    401 with a WWW-Authenticate challenge — not a 302 (which would
    indicate ``@authorized`` redirected an anonymous request because
    BasicAuthentication wasn't applied at all)."""
    creds = b64encode(f'alice:{_PWD_WRONG}'.encode()).decode('ascii')
    client = Client()
    with _enable_auth():
        response = client.get(
            '/api/v2/assets',
            HTTP_AUTHORIZATION=f'Basic {creds}',
        )
    assert response.status_code == 401
    # DRF's WWW-Authenticate challenge always names the FIRST configured
    # authenticator (DEFAULT_AUTHENTICATION_CLASSES order), not whichever
    # one actually rejected the request — see
    # APIView.get_authenticate_header. AnthiasAPITokenAuthentication is
    # listed first (the recommended path for new integrations), so every
    # 401 now advertises "Bearer" regardless of which scheme was tried.
    assert response.headers.get('WWW-Authenticate', '') == 'Bearer'


@pytest.mark.django_db
def test_basic_auth_deprecation_log_throttled(
    authed_operator: User, caplog: pytest.LogCaptureFixture
) -> None:
    """The DEPRECATED Basic-auth log must fire at most once per
    (user, client_ip, path) within the throttle window — a polling
    Anthias-CLI hitting the same endpoint every few seconds would
    otherwise flood the log with identical warnings."""
    import logging

    from anthias_server.lib import auth as auth_module

    # Clear any state left by other tests sharing the in-process
    # throttle dict.
    auth_module._basic_auth_log_seen.clear()

    creds = b64encode(f'alice:{_PWD_TOKEN_USER}'.encode()).decode('ascii')
    client = Client()
    with _enable_auth(), caplog.at_level(logging.WARNING):
        for _ in range(5):
            response = client.get(
                '/api/v2/assets',
                HTTP_AUTHORIZATION=f'Basic {creds}',
            )
            assert response.status_code == 200

    deprecated = [
        r
        for r in caplog.records
        if 'DEPRECATED: HTTP Basic auth' in r.getMessage()
    ]
    # Exactly one warning across all five identical requests.
    assert len(deprecated) == 1, (
        f'expected 1 DEPRECATED log line, got {len(deprecated)}'
    )

    # Different client_ip should not be throttled by the previous
    # entry — a new tuple gets its own log line. Use TEST-NET-1
    # (RFC 5737) so the value is unambiguously a documentation/test
    # placeholder and Sonar's hardcoded-IP hotspot doesn't fire.
    with _enable_auth(), caplog.at_level(logging.WARNING):
        client.get(
            '/api/v2/assets',
            HTTP_AUTHORIZATION=f'Basic {creds}',
            REMOTE_ADDR='192.0.2.42',  # NOSONAR (RFC 5737 doc IP)
        )
    deprecated_after = [
        r
        for r in caplog.records
        if 'DEPRECATED: HTTP Basic auth' in r.getMessage()
    ]
    assert len(deprecated_after) == 2


@pytest.mark.django_db
def test_auth_disabled_ignores_drf_authenticators(
    authed_operator: User,
) -> None:
    """When ``settings['auth_backend'] == ''`` (auth disabled), the
    documented contract is "API is fully open". DRF's stock auth
    classes would violate that — ``SessionAuthentication`` raises
    403 on unsafe methods without ``X-CSRFToken``,
    ``BasicAuthentication`` raises 401 on a malformed header. The
    Anthias-flavoured wrappers (``GatedSessionAuthentication``,
    ``DeprecatedBasicAuthentication``) both inherit
    ``_AuthBackendGated`` which returns ``None`` early when auth is
    disabled, so neither rejection fires.

    This test asserts both shapes pass through to a 200, which is
    impossible with stock DRF classes — the previous wiring would
    have returned 401 on the wrong-Basic-creds case.
    """
    client = Client()
    # auth_backend is '' by default in tests; do NOT enter
    # ``_enable_auth()`` here, that's the whole point.

    # 1. Wrong Basic-auth header. Stock BasicAuthentication would 401.
    creds = b64encode(f'alice:{_PWD_WRONG}'.encode()).decode('ascii')
    response = client.get(
        '/api/v2/assets', HTTP_AUTHORIZATION=f'Basic {creds}'
    )
    assert response.status_code == 200

    # 2. Authenticated session + POST without CSRF token. Stock
    #    SessionAuthentication.enforce_csrf would 403.
    client.force_login(authed_operator)
    response = client.post(
        '/api/v2/assets',
        data='{}',
        content_type='application/json',
    )
    # Either a normal 4xx for body-shape (no name, etc.) or a 200 —
    # but explicitly NOT 403 (the CSRF rejection we're guarding
    # against). The view dispatches and the auth/CSRF gate is silent.
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# AnthiasAPITokenAuthentication


@pytest.mark.django_db
def test_bearer_token_authenticates_even_when_auth_backend_disabled() -> None:
    """A deliberately issued token must keep working regardless of the
    operator's Basic/Session auth_backend toggle — that's the whole
    point of not inheriting ``_AuthBackendGated`` (see the class
    docstring). auth_backend is '' by default in tests; do NOT enter
    ``_enable_auth()`` here."""
    operator = _make_operator(pwd=_PWD_TOKEN_USER)
    _, raw_token = auth.issue_api_token(operator, 'fleet-server')

    client = Client()
    response = client.get(
        '/api/v2/assets', HTTP_AUTHORIZATION=f'Bearer {raw_token}'
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.django_db
def test_bearer_token_authenticates_when_auth_backend_enabled() -> None:
    operator = _make_operator(pwd=_PWD_TOKEN_USER)
    _, raw_token = auth.issue_api_token(operator, 'fleet-server')

    client = Client()
    with _enable_auth():
        response = client.get(
            '/api/v2/assets', HTTP_AUTHORIZATION=f'Bearer {raw_token}'
        )
    assert response.status_code == 200


@pytest.mark.django_db
def test_bearer_token_rejects_unknown_token() -> None:
    client = Client()
    response = client.get(
        '/api/v2/assets',
        HTTP_AUTHORIZATION=f'Bearer {auth.generate_api_token()}',
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    'header_value',
    [
        'Bearer',  # missing token
        'Bearer a b',  # extra whitespace/segment
    ],
)
@pytest.mark.django_db
def test_bearer_token_rejects_malformed_header(header_value: str) -> None:
    client = Client()
    response = client.get('/api/v2/assets', HTTP_AUTHORIZATION=header_value)
    assert response.status_code == 401


@pytest.mark.django_db
def test_bearer_token_rejects_expired_token() -> None:
    from datetime import timedelta

    from django.utils import timezone

    from anthias_server.api.models import AnthiasAPIToken

    operator = _make_operator(pwd=_PWD_TOKEN_USER)
    token_row, raw_token = auth.issue_api_token(operator, 'fleet-server')
    AnthiasAPIToken.objects.filter(pk=token_row.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    client = Client()
    response = client.get(
        '/api/v2/assets', HTTP_AUTHORIZATION=f'Bearer {raw_token}'
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_bearer_token_revoke_deletes_row_and_rejects_future_requests() -> None:
    from anthias_server.api.models import AnthiasAPIToken

    operator = _make_operator(pwd=_PWD_TOKEN_USER)
    token_row, raw_token = auth.issue_api_token(operator, 'fleet-server')
    token_row.delete()

    client = Client()
    response = client.get(
        '/api/v2/assets', HTTP_AUTHORIZATION=f'Bearer {raw_token}'
    )
    assert response.status_code == 401
    assert not AnthiasAPIToken.objects.filter(pk=token_row.pk).exists()


@pytest.mark.django_db
def test_bearer_token_updates_last_used_at() -> None:
    operator = _make_operator(pwd=_PWD_TOKEN_USER)
    token_row, raw_token = auth.issue_api_token(operator, 'fleet-server')
    assert token_row.last_used_at is None

    client = Client()
    response = client.get(
        '/api/v2/assets', HTTP_AUTHORIZATION=f'Bearer {raw_token}'
    )
    assert response.status_code == 200

    token_row.refresh_from_db()
    assert token_row.last_used_at is not None


# ---------------------------------------------------------------------------
# require_setup_complete — the mandatory first-run gate


def _make_staff_user(username: str, pwd: str) -> User:
    """A non-admin user with Settings access (is_staff=True,
    is_superuser=False) — the "granted access" tier from item 1 of the
    multi-user permission model, distinct from both a full admin
    (``_make_operator``) and a plain user (``_make_user``)."""
    return User.objects.create_user(
        username=username,
        password=pwd,  # NOSONAR
        is_staff=True,
    )


def test_require_setup_complete_redirects_when_no_admin(
    db: None,
) -> None:
    @require_setup_complete
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    response = view(factory.get('/'))
    assert isinstance(response, HttpResponse)
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:setup')


@pytest.mark.django_db
def test_require_setup_complete_passes_through_when_admin_exists() -> None:
    _make_operator()

    @require_setup_complete
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    assert view(factory.get('/')) == 'ok'


@pytest.mark.django_db
def test_require_setup_complete_ignores_auth_backend(
    monkeypatch: Any,
) -> None:
    """Unlike ``authorized``, this gate has no "disabled" bypass — it
    must redirect even when ``auth_backend`` is enabled, because the
    thing missing is an admin account, not a session."""
    fake_settings = {'auth_backend': 'auth_basic'}
    monkeypatch.setattr('anthias_server.settings.settings', fake_settings)

    @require_setup_complete
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    response = view(factory.get('/'))
    assert isinstance(response, HttpResponse)
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:setup')


@pytest.mark.django_db
def test_setup_view_reachable_without_admin() -> None:
    response = Client().get(reverse('anthias_app:setup'))
    assert response.status_code == 200
    assert 'Create admin account' in response.content.decode()


@pytest.mark.django_db
def test_home_redirects_to_setup_when_no_admin() -> None:
    response = Client().get(reverse('anthias_app:home'))
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:setup')


@pytest.mark.django_db
def test_home_reachable_once_admin_exists() -> None:
    _make_operator()
    response = Client().get(reverse('anthias_app:home'))
    assert response.status_code == 200


@pytest.mark.django_db
def test_splash_page_stays_reachable_without_admin() -> None:
    """The boot-time on-screen IP display must keep working pre-setup
    — it's how an operator finds the device to go run setup on it."""
    response = Client().get(reverse('anthias_app:splash_page'))
    assert response.status_code == 200


@pytest.mark.django_db
def test_login_stays_reachable_without_admin() -> None:
    """Left unguarded (same as before this feature) rather than bounced
    to /setup/ — harmless since authenticate() can't succeed against
    an empty User table anyway."""
    response = Client().get(reverse('anthias_app:login'))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# setup view — first admin creation


@pytest.mark.django_db
def test_setup_creates_admin_logs_in_and_pins_auth_backend(
    tmp_path: Any,
) -> None:
    from anthias_server.settings import settings as device_settings

    # Isolate settings.save() to a throwaway conf file — the setup
    # view flips auth_backend to 'auth_basic', and without this the
    # write lands on the real ~/.anthias/anthias.conf and leaks into
    # every other test in the run (same isolation
    # test_template_views.py's ``_isolated_settings_conf`` fixture
    # provides for settings-mutating tests there).
    original_conf_file = device_settings.conf_file
    try:
        device_settings.conf_file = str(tmp_path / 'anthias.conf')
        device_settings.save()

        client = Client()
        response = client.post(
            reverse('anthias_app:setup'),
            {
                'username': 'first-admin',
                'password': _PWD_INITIAL,
                'password_confirm': _PWD_INITIAL,
            },
        )
        assert response.status_code == 302
        assert response['Location'] == reverse('anthias_app:home')

        user = User.objects.get(username='first-admin')
        assert user.is_superuser and user.is_staff and user.is_active
        assert user.check_password(_PWD_INITIAL)

        device_settings.load()
        assert device_settings['auth_backend'] == 'auth_basic'

        # The wizard also signed the new admin in.
        session_response = client.get(reverse('anthias_app:home'))
        assert session_response.status_code == 200
    finally:
        device_settings.conf_file = original_conf_file
        device_settings.load()


@pytest.mark.django_db
def test_setup_rejects_password_mismatch() -> None:
    client = Client()
    response = client.post(
        reverse('anthias_app:setup'),
        {
            'username': 'first-admin',
            'password': _PWD_MISMATCH_A,
            'password_confirm': _PWD_MISMATCH_B,
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(username='first-admin').exists()


@pytest.mark.django_db
def test_setup_redirects_to_home_once_admin_exists() -> None:
    """A stale bookmark / second tab / back-button visit to /setup/
    after it's already run must not re-show (or re-submit) the
    wizard."""
    _make_operator()
    response = Client().get(reverse('anthias_app:setup'))
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:home')


# ---------------------------------------------------------------------------
# require_settings_access — the Settings-family permission gate


def _attach_message_storage(request: Any) -> None:
    """require_settings_access calls messages.error() on a block —
    outside the full middleware stack (a bare RequestFactory request,
    same as elsewhere in this file), Django's messages framework needs
    a storage backend attached first or it raises MessageFailure.
    Standard Django test-only wiring, mirrors what SessionMiddleware +
    MessageMiddleware do for real requests."""
    from django.contrib.messages.storage.fallback import FallbackStorage

    request.session = {}
    request._messages = FallbackStorage(request)


def test_require_settings_access_blocks_anonymous(monkeypatch: Any) -> None:
    @require_settings_access
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/settings/')
    request.user = MagicMock(is_authenticated=False)
    _attach_message_storage(request)
    response = view(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:home')


def test_require_settings_access_blocks_non_staff_authenticated() -> None:
    @require_settings_access
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/settings/')
    request.user = MagicMock(
        is_authenticated=True, is_staff=False, is_superuser=False
    )
    _attach_message_storage(request)
    response = view(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:home')


def test_require_settings_access_allows_staff() -> None:
    @require_settings_access
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/settings/')
    request.user = MagicMock(
        is_authenticated=True, is_staff=True, is_superuser=False
    )
    assert view(request) == 'ok'


def test_require_settings_access_allows_superuser() -> None:
    @require_settings_access
    def view(request: Any) -> str:
        return 'ok'

    factory = RequestFactory()
    request = factory.get('/settings/')
    request.user = MagicMock(
        is_authenticated=True, is_staff=False, is_superuser=True
    )
    assert view(request) == 'ok'


@pytest.mark.django_db
def test_settings_page_blocks_non_staff_authenticated_user() -> None:
    _make_operator()  # satisfies require_setup_complete
    plain_user = _make_user(username='plain', pwd=_PWD_THROWAWAY_1)
    client = Client()
    client.force_login(plain_user)

    response = client.get(reverse('anthias_app:settings'))
    assert response.status_code == 302
    assert response['Location'] == reverse('anthias_app:home')


@pytest.mark.django_db
def test_settings_page_allows_staff_user_without_admin(
    monkeypatch: Any,
) -> None:
    # /proc/cpuinfo only exists on Linux; stub the one Linux-specific
    # call settings_view's context builder makes so the real view +
    # middleware + template stack can run on any host (same stub
    # test_api_tokens_view.py uses).
    monkeypatch.setattr(
        'anthias_server.app.page_context.device_helper.parse_cpu_info',
        lambda: {'cpu_count': 0},
    )
    _make_operator()  # satisfies require_setup_complete
    staff_user = _make_staff_user(username='staffer', pwd=_PWD_THROWAWAY_1)
    client = Client()
    client.force_login(staff_user)

    response = client.get(reverse('anthias_app:settings'))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# apply_self_account_changes — HTML self-service password/username change


@pytest.mark.django_db
def test_apply_self_account_changes_updates_own_password() -> None:
    user = _make_staff_user(username='staffer', pwd=_PWD_OLD)
    request = _request_with_user(user)

    apply_self_account_changes(
        request,
        current_pwd=_PWD_OLD,
        new_username='',
        new_pwd=_PWD_NEW,
        new_pwd_confirm=_PWD_NEW,
    )
    user.refresh_from_db()
    assert user.check_password(_PWD_NEW)


@pytest.mark.django_db
def test_apply_self_account_changes_requires_current_password() -> None:
    user = _make_staff_user(username='staffer', pwd=_PWD_OLD)
    request = _request_with_user(user)

    with pytest.raises(
        AuthSettingsError, match='supply current password to change password'
    ):
        apply_self_account_changes(
            request,
            current_pwd='',
            new_username='',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
        )


@pytest.mark.django_db
def test_apply_self_account_changes_rejects_wrong_current_password() -> None:
    user = _make_staff_user(username='staffer', pwd=_PWD_OLD)
    request = _request_with_user(user)

    with pytest.raises(AuthSettingsError, match='Incorrect current password'):
        apply_self_account_changes(
            request,
            current_pwd=_PWD_WRONG,
            new_username='',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
        )


@pytest.mark.django_db
def test_apply_self_account_changes_rejects_username_collision() -> None:
    _make_user(username='taken', pwd=_PWD_THROWAWAY_1)
    user = _make_staff_user(username='staffer', pwd=_PWD_OLD)
    request = _request_with_user(user)

    with pytest.raises(AuthSettingsError, match='already taken'):
        apply_self_account_changes(
            request,
            current_pwd=_PWD_OLD,
            new_username='taken',
            new_pwd='',
            new_pwd_confirm='',
        )


@pytest.mark.django_db
def test_apply_self_account_changes_raises_when_not_authenticated() -> None:
    request = _request_with_user(MagicMock(is_authenticated=False))

    with pytest.raises(AuthSettingsError, match='must be signed in'):
        apply_self_account_changes(
            request,
            current_pwd='',
            new_username='',
            new_pwd=_PWD_NEW,
            new_pwd_confirm=_PWD_NEW,
        )


@pytest.mark.django_db
def test_apply_self_account_changes_operates_on_request_user_not_operator() -> (
    None
):
    """Unlike ``apply_auth_settings``, this is NOT restricted to "the"
    canonical operator — any logged-in user (e.g. a second admin, or a
    staff-granted non-admin) can change their own account."""
    _make_operator(username='canonical-operator', pwd=_PWD_OLD)
    second_admin = User.objects.create_superuser(
        username='second-admin', password=_PWD_OLD
    )
    request = _request_with_user(second_admin)

    apply_self_account_changes(
        request,
        current_pwd=_PWD_OLD,
        new_username='',
        new_pwd=_PWD_NEW,
        new_pwd_confirm=_PWD_NEW,
    )
    second_admin.refresh_from_db()
    assert second_admin.check_password(_PWD_NEW)


@pytest.mark.django_db
def test_settings_save_self_password_change_still_works(
    tmp_path: Any,
) -> None:
    """End-to-end regression for the self-service path settings_save
    now routes through apply_self_account_changes instead of the
    retired apply_auth_settings call."""
    from anthias_server.settings import settings as device_settings

    original_conf_file = device_settings.conf_file
    try:
        device_settings.conf_file = str(tmp_path / 'anthias.conf')
        device_settings.save()

        admin = _make_operator(pwd=_PWD_OLD)
        client = Client()
        client.force_login(admin)

        with patch(
            'anthias_server.settings.ViewerPublisher.send_to_viewer',
            return_value=None,
        ):
            response = client.post(
                reverse('anthias_app:settings_save'),
                {
                    'player_name': 'Test',
                    'default_duration': '10',
                    'default_streaming_duration': '300',
                    'audio_output': 'hdmi',
                    'date_format': 'mm/dd/yyyy',
                    'current_password': _PWD_OLD,
                    'password': _PWD_NEW,
                    'password_2': _PWD_NEW,
                },
            )
        assert response.status_code == 302
        admin.refresh_from_db()
        assert admin.check_password(_PWD_NEW)
    finally:
        device_settings.conf_file = original_conf_file
        device_settings.load()


# ---------------------------------------------------------------------------
# _is_last_admin


@pytest.mark.django_db
def test_is_last_admin_true_for_sole_admin() -> None:
    admin = _make_operator()
    assert _is_last_admin(admin) is True


@pytest.mark.django_db
def test_is_last_admin_false_when_another_admin_exists() -> None:
    admin_a = _make_operator(username='a', pwd=_PWD_THROWAWAY_1)
    User.objects.create_superuser(username='b', password=_PWD_THROWAWAY_2)
    assert _is_last_admin(admin_a) is False


@pytest.mark.django_db
def test_is_last_admin_false_for_non_admin() -> None:
    plain_user = _make_user(username='plain', pwd=_PWD_THROWAWAY_1)
    assert _is_last_admin(plain_user) is False


# ---------------------------------------------------------------------------
# User CRUD views (Settings > Users)


@pytest.mark.django_db
def test_user_create_requires_admin() -> None:
    staff_user = _make_staff_user(username='staffer', pwd=_PWD_THROWAWAY_1)
    client = Client()
    client.force_login(staff_user)

    client.post(
        reverse('anthias_app:user_create'),
        {'username': 'newbie', 'password': _PWD_NEW},
    )
    assert not User.objects.filter(username='newbie').exists()


@pytest.mark.django_db
def test_user_create_by_admin_defaults_no_settings_access() -> None:
    admin = _make_operator()
    client = Client()
    client.force_login(admin)

    response = client.post(
        reverse('anthias_app:user_create'),
        {'username': 'newbie', 'password': _PWD_NEW},
    )
    assert response.status_code == 302
    new_user = User.objects.get(username='newbie')
    assert new_user.check_password(_PWD_NEW)
    assert not new_user.is_staff
    assert not new_user.is_superuser


@pytest.mark.django_db
def test_user_create_grant_admin_creates_admin() -> None:
    admin = _make_operator()
    client = Client()
    client.force_login(admin)

    client.post(
        reverse('anthias_app:user_create'),
        {
            'username': 'newadmin',
            'password': _PWD_NEW,
            'is_superuser': 'true',
        },
    )
    new_admin = User.objects.get(username='newadmin')
    assert new_admin.is_superuser
    assert new_admin.is_staff


@pytest.mark.django_db
def test_user_create_rejects_taken_username() -> None:
    admin = _make_operator(username='alice', pwd=_PWD_OLD)
    client = Client()
    client.force_login(admin)

    client.post(
        reverse('anthias_app:user_create'),
        {'username': 'alice', 'password': _PWD_NEW},
    )
    # Still exactly one 'alice' — the collision was rejected, not
    # silently overwritten or duplicated.
    assert User.objects.filter(username='alice').count() == 1


@pytest.mark.django_db
def test_user_edit_promote_and_demote() -> None:
    admin = _make_operator(username='admin', pwd=_PWD_OLD)
    other_admin = User.objects.create_superuser(
        username='other-admin', password=_PWD_THROWAWAY_1
    )
    plain_user = _make_user(username='plain', pwd=_PWD_THROWAWAY_2)
    client = Client()
    client.force_login(admin)

    client.post(
        reverse('anthias_app:user_edit', args=[plain_user.pk]),
        {'action': 'promote'},
    )
    plain_user.refresh_from_db()
    assert plain_user.is_superuser and plain_user.is_staff

    client.post(
        reverse('anthias_app:user_edit', args=[other_admin.pk]),
        {'action': 'demote'},
    )
    other_admin.refresh_from_db()
    assert not other_admin.is_superuser


@pytest.mark.django_db
def test_user_edit_cant_demote_last_admin() -> None:
    admin = _make_operator()
    client = Client()
    client.force_login(admin)

    client.post(
        reverse('anthias_app:user_edit', args=[admin.pk]),
        {'action': 'demote'},
    )
    admin.refresh_from_db()
    assert admin.is_superuser


@pytest.mark.django_db
def test_user_edit_set_staff_toggle() -> None:
    admin = _make_operator()
    plain_user = _make_user(username='plain', pwd=_PWD_THROWAWAY_1)
    client = Client()
    client.force_login(admin)

    client.post(
        reverse('anthias_app:user_edit', args=[plain_user.pk]),
        {'action': 'set_staff', 'is_staff': 'true'},
    )
    plain_user.refresh_from_db()
    assert plain_user.is_staff

    client.post(
        reverse('anthias_app:user_edit', args=[plain_user.pk]),
        {'action': 'set_staff', 'is_staff': 'false'},
    )
    plain_user.refresh_from_db()
    assert not plain_user.is_staff


@pytest.mark.django_db
def test_user_edit_cant_toggle_staff_on_admin_row() -> None:
    admin = _make_operator()
    client = Client()
    client.force_login(admin)

    client.post(
        reverse('anthias_app:user_edit', args=[admin.pk]),
        {'action': 'set_staff', 'is_staff': 'false'},
    )
    admin.refresh_from_db()
    # Admins always have Settings access regardless of is_staff.
    assert admin.is_staff


@pytest.mark.django_db
def test_user_edit_requires_admin() -> None:
    staff_user = _make_staff_user(username='staffer', pwd=_PWD_THROWAWAY_1)
    plain_user = _make_user(username='plain', pwd=_PWD_THROWAWAY_2)
    client = Client()
    client.force_login(staff_user)

    client.post(
        reverse('anthias_app:user_edit', args=[plain_user.pk]),
        {'action': 'promote'},
    )
    plain_user.refresh_from_db()
    assert not plain_user.is_superuser


@pytest.mark.django_db
def test_user_reset_password_by_admin() -> None:
    admin = _make_operator()
    target = _make_user(username='plain', pwd=_PWD_OLD)
    client = Client()
    client.force_login(admin)

    response = client.post(
        reverse('anthias_app:user_reset_password', args=[target.pk])
    )
    assert response.status_code == 302
    target.refresh_from_db()
    assert not target.check_password(_PWD_OLD)

    stashed = client.session['reset_user_password']
    assert stashed['username'] == 'plain'
    assert target.check_password(stashed['raw'])


@pytest.mark.django_db
def test_user_reset_password_requires_admin() -> None:
    staff_user = _make_staff_user(username='staffer', pwd=_PWD_THROWAWAY_1)
    target = _make_user(username='plain', pwd=_PWD_OLD)
    client = Client()
    client.force_login(staff_user)

    client.post(reverse('anthias_app:user_reset_password', args=[target.pk]))
    target.refresh_from_db()
    assert target.check_password(_PWD_OLD)


@pytest.mark.django_db
def test_user_reset_password_blocked_for_self() -> None:
    """An admin can't use the no-current-password reset path on their
    own account — that would bypass the current-password check the
    self-service flow enforces. They must use "Your account" instead."""
    admin = _make_operator(pwd=_PWD_OLD)
    client = Client()
    client.force_login(admin)

    client.post(reverse('anthias_app:user_reset_password', args=[admin.pk]))
    admin.refresh_from_db()
    assert admin.check_password(_PWD_OLD)


@pytest.mark.django_db
def test_user_delete_by_admin() -> None:
    admin = _make_operator()
    target = _make_user(username='plain', pwd=_PWD_OLD)
    client = Client()
    client.force_login(admin)

    response = client.post(
        reverse('anthias_app:user_delete', args=[target.pk])
    )
    assert response.status_code == 302
    assert not User.objects.filter(pk=target.pk).exists()


@pytest.mark.django_db
def test_user_delete_allows_deleting_a_non_last_admin() -> None:
    """Deleting a co-admin (not yourself) is allowed as long as you —
    the caller, who must themselves be an admin to reach this view at
    all — remain, so the target is never actually "the last admin" in
    that shape of request. The only HTTP-reachable path where
    ``_is_last_admin`` actually blocks something is the sole-admin
    self-delete case, covered by ``test_user_delete_blocked_for_self``
    (self-delete is blocked unconditionally, before the last-admin
    check even runs); ``_is_last_admin`` itself is unit-tested
    directly above.
    """
    admin = _make_operator()
    client = Client()
    client.force_login(admin)

    other_admin = User.objects.create_superuser(
        username='other-admin', password=_PWD_THROWAWAY_1
    )
    client.post(reverse('anthias_app:user_delete', args=[other_admin.pk]))
    assert not User.objects.filter(pk=other_admin.pk).exists()
    # The caller (still an admin) survives.
    assert User.objects.filter(pk=admin.pk).exists()


@pytest.mark.django_db
def test_user_delete_blocked_for_self() -> None:
    admin = _make_operator()
    User.objects.create_superuser(
        username='other-admin', password=_PWD_THROWAWAY_1
    )
    client = Client()
    client.force_login(admin)

    client.post(reverse('anthias_app:user_delete', args=[admin.pk]))
    assert User.objects.filter(pk=admin.pk).exists()


@pytest.mark.django_db
def test_user_delete_requires_admin() -> None:
    staff_user = _make_staff_user(username='staffer', pwd=_PWD_THROWAWAY_1)
    target = _make_user(username='plain', pwd=_PWD_OLD)
    client = Client()
    client.force_login(staff_user)

    client.post(reverse('anthias_app:user_delete', args=[target.pk]))
    assert User.objects.filter(pk=target.pk).exists()
