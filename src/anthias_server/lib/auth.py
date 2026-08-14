"""Auth helpers built on top of django.contrib.auth.

The legacy ``Auth`` / ``NoAuth`` / ``BasicAuth`` abstractions have been
retired in favour of Django's built-in primitives. Anthias now has
four credential paths, each with a distinct caller and trust model:

1. **Browser session** (operators using the dashboard).
   Driven by ``django.contrib.auth``: the login form posts to
   :func:`anthias_server.app.views.login`, which calls
   ``authenticate()`` + ``login()``. The resulting session cookie
   gates both the HTML views (via :func:`authorized`) and the DRF
   API (via DRF's ``SessionAuthentication``).

2. **API bearer token** (unattended integrations — the recommended
   path for new callers).
   A UI-managed, per-integration ``AnthiasAPIToken`` row, presented as
   ``Authorization: Bearer <token>``. Scoped to a single ``User`` (so
   it inherits that user's permissions), stored only as a salted hash,
   revocable independently of the operator's own password. This is
   what a fleet-management server (or any other unattended caller)
   should use instead of Basic auth against the operator's own
   credentials.

3. **HTTP Basic** (legacy headless path, kept for back-compat).
   DRF's stock ``BasicAuthentication`` against the same User table,
   wrapped to log a ``DEPRECATED`` warning when Basic auth is used.
   The log is throttled per ``(user, IP, path)`` tuple with a 1-hour
   TTL so a chatty polling client (e.g. Anthias-CLI hitting
   ``/api/v2/info`` every few seconds) doesn't flood the log; the
   signal we care about is "this caller is still on Basic", not the
   request rate. Pre-2826 versions of Anthias-CLI and any third-party
   scripts that were written against the old auth keep working
   unchanged, but new integrations should use the bearer-token path
   above instead.

4. **Viewer ↔ server shared secret** (intra-device, same trust
   boundary).
   The viewer process can't carry an operator session, but it does
   need to call a small set of internal endpoints (currently just
   ``AssetRecheckViewV2``). It signs requests with an HMAC of
   ``settings['django_secret_key']`` and sends the digest in
   ``X-Anthias-Internal-Token``; the server verifies via
   :func:`anthias_common.internal_auth.is_internal_request`. This is
   *not* a user-facing credential — it bypasses the User table
   entirely and is only safe because the secret never leaves the
   device. New endpoints that the viewer needs to call should gate
   on ``is_internal_request`` directly rather than going through
   :func:`authorized`.

This module's surface is:

* ``hash_password`` / ``verify_password`` — thin shims over Django's
  hashers, kept so callers don't have to import them on every site
  and so the data migration can sniff for non-Django-format strings
  in ``anthias.conf`` before promoting them into ``User.password``.
* ``generate_api_token`` / ``hash_api_token`` / ``issue_api_token`` —
  create and store ``AnthiasAPIToken`` rows. The raw token is only
  ever available at ``issue_api_token`` call time; everything
  afterwards works off ``hash_api_token``'s digest.
* ``AnthiasAPITokenAuthentication`` — ``Authorization: Bearer``
  against ``AnthiasAPIToken``. Deliberately not gated by
  ``auth_backend`` (see its class docstring).
* ``DeprecatedBasicAuthentication`` — DRF's ``BasicAuthentication``
  with a throttled ``logger.warning`` (one line per ``(user, IP,
  path)`` per ``_BASIC_AUTH_LOG_TTL_S``) so production logs surface
  the last callers still using the legacy header without being
  flooded by chatty polling clients. Also gated by ``auth_backend``
  (no-ops when the operator has turned auth off).
* ``GatedSessionAuthentication`` — DRF's ``SessionAuthentication``
  with the same auth-backend gate so an incidental session cookie
  doesn't trigger CSRF rejection on write endpoints when auth is
  disabled.
* ``authorized`` — feature-flagged ``@login_required``. Bypasses when
  the operator turned auth off (``settings['auth_backend'] == ''``)
  and otherwise redirects to the login page with the request's
  original path round-tripped through ``?next=``.
* ``require_setup_complete`` — the mandatory first-run gate. Unlike
  ``authorized`` it is NOT feature-flagged on ``auth_backend`` (there
  is no "disabled" state once a device has an admin account) —
  it redirects to ``/setup/`` whenever no ``User`` with
  ``is_superuser=True`` exists yet. HTML-app-only: never applied to
  the DRF API, whose "auth disabled = fully open" wire contract this
  feature does not change.
* ``require_settings_access`` — second gate for the Settings-family
  HTML views: authenticated is not enough, ``request.user`` must also
  be an admin (``is_superuser``) or have been granted Settings access
  (``is_staff``, repurposed for this — see ``AnthiasApp`` docs).
* ``apply_auth_settings`` / ``operator_username`` — settings-page
  helpers shared by the HTML and DRF write paths. Kept intact
  (including the now-HTML-unreachable ``auth_backend`` switching
  logic) because ``DeviceSettingsViewV2.patch`` (the v2 API) still
  depends on the exact same contract — the API's wire shape must not
  change here.
* ``apply_self_account_changes`` — the HTML-only self-service
  username/password change any logged-in user (not just "the"
  canonical operator) can make for their own account, current-password
  required. Distinct from ``apply_auth_settings`` (API, canonical
  operator) and from an admin's no-current-password-needed reset of
  *someone else's* password (``anthias_server.app.views.user_reset_password``).
"""

from __future__ import annotations

import logging
import os.path
import re
from collections.abc import Callable
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    ParamSpec,
    TypeVar,
    cast,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest, HttpResponse
    from rest_framework.request import Request as DRFRequest

    # ``apply_auth_settings`` and ``_operator_user`` are called from
    # both the Django HTML write path (``HttpRequest``) and the DRF
    # API write path (``rest_framework.request.Request``). DRF's
    # Request wraps the underlying Django request and delegates
    # ``.user``, so the body of these helpers handles both shapes
    # the same way; the annotation just needs to admit either one.
    # Spelled as an explicit ``TypeAlias`` so mypy always resolves the
    # forward-ref ``'AnyRequest'`` as a type rather than a module
    # variable — the implicit form flipped to "not valid as a type"
    # once settings.py started importing this module (ANTHIAS-3D).
    type AnyRequest = HttpRequest | DRFRequest

P = ParamSpec('P')
R = TypeVar('R')

LINUX_USER = os.getenv('USER', 'pi')

# Legacy hashes are bare 64-char hex SHA256 digests (no algorithm prefix).
# Django's make_password() output is always prefixed (e.g. "pbkdf2_sha256$...")
# so the two formats are unambiguously distinguishable. Used by the
# 0005 data migration to spot un-migratable rows; kept here so the
# regex has one home.
_LEGACY_SHA256_HEX = re.compile(r'^[0-9a-f]{64}$')

logger = logging.getLogger(__name__)


def _is_legacy_sha256(stored: str) -> bool:
    return bool(_LEGACY_SHA256_HEX.match(stored))


def hash_password(password: str) -> str:
    """Hash a password using Django's default (PBKDF2-SHA256)."""
    from django.contrib.auth.hashers import make_password

    return str(make_password(password))


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against a Django-format stored hash."""
    if not stored:
        return False
    from django.contrib.auth.hashers import check_password

    return bool(check_password(password, stored))


# Prefix on every issued token, both to make a leaked value greppable
# in logs/history and so the UI's "ant_a1b2c3…" display is recognisably
# an Anthias credential (GitHub/Stripe PAT convention).
_API_TOKEN_PREFIX = 'ant_'
# Length of the raw-token slice (including the ``ant_`` prefix) shown
# in clear in the UI/admin — enough to tell rows apart, short enough
# that it isn't a meaningful fraction of the secret.
_API_TOKEN_DISPLAY_PREFIX_LEN = 12


def generate_api_token() -> str:
    """A new random bearer token, e.g. ``ant_<43 url-safe chars>``.

    ``secrets.token_urlsafe(32)`` gives 256 bits of entropy — the same
    budget GitHub/Stripe use for personal access tokens.
    """
    import secrets

    return f'{_API_TOKEN_PREFIX}{secrets.token_urlsafe(32)}'


def hash_api_token(raw_token: str) -> str:
    """SHA-256 hex digest of a raw token, for storage/lookup.

    Unlike passwords, API tokens are already high-entropy random
    strings, not user-chosen — a slow, salted password hasher (PBKDF2)
    buys nothing here and would make every authenticated API request
    pay its cost. A fast digest is the right primitive, same as
    GitHub/Stripe's own token storage.
    """
    import hashlib

    return hashlib.sha256(raw_token.encode()).hexdigest()


def issue_api_token(
    user: User, name: str, *, purpose: str = 'general'
) -> tuple[Any, str]:
    """Create and persist a new ``AnthiasAPIToken`` for ``user``.

    Returns ``(token_row, raw_token)`` — the raw value is never stored
    and this is the only place it's ever available; the caller must
    show it to the operator immediately, it can't be recovered later.

    ``purpose='fleet_management'`` marks this as *the* token a fleet
    server pairs with (see ``is_fleet_managed``/``fleet_management_token``
    below) — a player is paired with at most one fleet server at a
    time, so issuing a new fleet-management token replaces any
    existing one rather than accumulating alongside it. Callers that
    want that replace-not-accumulate behavior should delete any prior
    fleet-management token themselves before calling this; kept as the
    caller's responsibility rather than baked in here so this function
    stays a plain "create one token" primitive.
    """
    from anthias_server.api.models import AnthiasAPIToken

    raw_token = generate_api_token()
    token_row = AnthiasAPIToken.objects.create(
        user=user,
        name=name,
        token_hash=hash_api_token(raw_token),
        prefix=raw_token[:_API_TOKEN_DISPLAY_PREFIX_LEN],
        purpose=purpose,
    )
    return token_row, raw_token


def fleet_management_token() -> Any | None:
    """The single ``AnthiasAPIToken`` (if any) a fleet server paired
    with, or ``None`` if this player isn't fleet-managed."""
    from anthias_server.api.models import AnthiasAPIToken

    return AnthiasAPIToken.objects.filter(
        purpose=AnthiasAPIToken.PURPOSE_FLEET_MANAGEMENT
    ).first()


def is_fleet_managed() -> bool:
    return fleet_management_token() is not None


# Throttle window for the DEPRECATED-Basic-auth log line. The signal
# we want is "this caller is still on Basic" — knowing it once an
# hour per (user, IP, path) is enough to chase down stragglers, and
# it's much cheaper than firing one WARNING per request when a
# polling Anthias-CLI hits /api/v2/info every 10s.
#
# In-process dict is a singleton per uvicorn worker; multi-worker
# deploys may emit a few more lines than this nominally allows, but
# the bound stays per-worker and the cardinality is low (a single
# operator account, a small handful of LAN IPs and API paths). Worst
# case of a race-on-write is one extra log line, which is fine.
_BASIC_AUTH_LOG_TTL_S = 3600.0
_basic_auth_log_seen: dict[tuple[str, str, str], float] = {}


def _should_log_basic_auth_use(
    username: str, client_ip: str, path: str
) -> bool:
    """Per-(user, IP, path) throttle on the deprecation log line.

    Returns True the first time a particular tuple is seen (or after
    the TTL expires) and False during the throttle window.
    Side-effects update ``_basic_auth_log_seen``.
    """
    import time

    key = (username, client_ip, path)
    now = time.monotonic()
    expiry = _basic_auth_log_seen.get(key)
    if expiry is not None and expiry > now:
        return False
    _basic_auth_log_seen[key] = now + _BASIC_AUTH_LOG_TTL_S
    return True


def _build_drf_auth_classes() -> dict[str, type]:
    """Build the DRF auth classes lazily.

    DRF reaches for ``rest_framework`` at import time, which fails on
    the viewer process (it doesn't load ``rest_framework`` at all —
    see the ``ANTHIAS_SERVICE != 'viewer'`` branch in
    ``django_project.settings``). Wrapping the import in a factory
    means viewer ``import lib.auth`` doesn't pull DRF in.

    Returns all three:

    * ``DeprecatedBasicAuthentication`` — Basic auth + throttled
      deprecation warning.
    * ``GatedSessionAuthentication`` — DRF's stock
      ``SessionAuthentication`` with the ``_AuthBackendGated`` mixin.
    * ``AnthiasAPITokenAuthentication`` — ``Authorization: Bearer``
      against ``AnthiasAPIToken``, for unattended integrations (e.g. a
      fleet-management server calling into this device's API).

    The first two inherit ``_AuthBackendGated`` so they short-circuit
    when ``settings['auth_backend']`` is empty: the documented contract
    is "auth disabled = the API is fully open", which DRF's stock
    authenticators would otherwise violate. Stock ``SessionAuthentication``
    enforces CSRF whenever a session cookie is present (403 on unsafe
    methods without ``X-CSRFToken``); stock ``BasicAuthentication``
    returns 401 when an ``Authorization: Basic …`` header has wrong
    creds. Neither is appropriate when auth is turned off.

    ``AnthiasAPITokenAuthentication`` deliberately does NOT inherit
    ``_AuthBackendGated`` — a bearer token is an explicitly issued
    credential, independent of the operator's own password/session
    login. Gating it on ``auth_backend`` would silently break every
    integration holding a still-valid token the moment an operator
    disabled Basic/Session auth, which is a worse failure mode than
    "the token keeps working regardless of that toggle."
    """
    from rest_framework import exceptions
    from rest_framework.authentication import (
        BaseAuthentication,
        BasicAuthentication,
        SessionAuthentication,
        get_authorization_header,
    )

    class _AuthBackendGated:
        """Mixin: ``authenticate()`` returns ``None`` (= "this class
        doesn't recognise the request, try the next one") when the
        operator has turned auth off via ``settings['auth_backend']``.
        Layered on top of any DRF authenticator so the auth-disabled
        contract holds: when ``auth_backend == ''`` the API is fully
        open and credentials/cookies are simply ignored.
        """

        def authenticate(self, request):  # type: ignore[no-untyped-def]
            from anthias_server.settings import settings as device_settings

            if not device_settings['auth_backend']:
                return None
            return super().authenticate(request)  # type: ignore[misc]

    class DeprecatedBasicAuthentication(
        _AuthBackendGated, BasicAuthentication
    ):
        """``BasicAuthentication`` that logs a deprecation warning the
        first time it sees each ``(user, client_ip, path)`` tuple
        (and again after ``_BASIC_AUTH_LOG_TTL_S``) so we can grep
        production logs for the last surviving callers before
        removing the path entirely.

        Pre-2826 versions of Anthias-CLI sent ``Authorization: Basic
        <b64(user:pass)>`` to /api/v2/...; we keep accepting that
        header for back-compat but it's on the chopping block. The
        log line tells us which IP and which path is still using
        the old scheme; per-tuple throttling keeps the log signal
        clean without flooding when a polling client hammers a
        single endpoint.
        """

        def authenticate_credentials(  # type: ignore[no-untyped-def]
            self, userid, password, request=None
        ):
            result = super().authenticate_credentials(
                userid, password, request=request
            )
            # Mirror DRF's contract: success returns ``(user, None)``.
            # Only log on success so a rate of "Basic auth attempts"
            # doesn't dwarf the real signal of "Basic auth still in
            # production use".
            user, _ = result
            client_ip = (
                request.META.get('REMOTE_ADDR', 'unknown')
                if request is not None
                else 'unknown'
            )
            path = (
                getattr(request, 'path', 'unknown')
                if request is not None
                else 'unknown'
            )
            if _should_log_basic_auth_use(
                user.get_username(), client_ip, path
            ):
                logger.warning(
                    'DEPRECATED: HTTP Basic auth used on %s by user %r '
                    'from %s. The Basic auth path is retained for '
                    'back-compat only and will be removed in a future '
                    'release.',
                    path,
                    user.get_username(),
                    client_ip,
                )
            return result

    class GatedSessionAuthentication(_AuthBackendGated, SessionAuthentication):
        """``SessionAuthentication`` that no-ops when auth is disabled.

        DRF's stock class enforces CSRF on unsafe methods whenever a
        session cookie is present (writes 403 to writes without
        ``X-CSRFToken``). With ``auth_backend == ''`` we want the API
        to be fully open even for clients that incidentally carry a
        cookie — the mixin makes that happen by short-circuiting
        ``authenticate`` before the CSRF check runs.
        """

    class AnthiasAPITokenAuthentication(BaseAuthentication):
        """``Authorization: Bearer <token>`` against ``AnthiasAPIToken``.

        The recommended path for unattended integrations (a
        fleet-management server, a CI job, a monitoring probe) —
        anything that shouldn't hold the operator's own username and
        password. See the class docstring on ``_build_drf_auth_classes``
        for why this class does not gate on ``auth_backend``.
        """

        keyword = b'bearer'

        def authenticate(self, request):  # type: ignore[no-untyped-def]
            header = get_authorization_header(request).split()
            if not header or header[0].lower() != self.keyword:
                return None
            if len(header) != 2:
                raise exceptions.AuthenticationFailed(
                    'Invalid Authorization header. Expected "Bearer <token>".'
                )
            try:
                raw_token = header[1].decode()
            except UnicodeDecodeError as exc:
                raise exceptions.AuthenticationFailed(
                    'Invalid token header.'
                ) from exc
            return self._authenticate_token(raw_token)

        def _authenticate_token(  # type: ignore[no-untyped-def]
            self, raw_token: str
        ):
            from django.utils import timezone

            from anthias_server.api.models import AnthiasAPIToken

            try:
                token = AnthiasAPIToken.objects.select_related('user').get(
                    token_hash=hash_api_token(raw_token)
                )
            except AnthiasAPIToken.DoesNotExist as exc:
                raise exceptions.AuthenticationFailed(
                    'Invalid token.'
                ) from exc
            if token.expires_at is not None and token.expires_at <= (
                timezone.now()
            ):
                raise exceptions.AuthenticationFailed('Token expired.')
            if not token.user.is_active:
                raise exceptions.AuthenticationFailed(
                    'User inactive or deleted.'
                )
            # Best-effort last-used bookkeeping — an UPDATE on the
            # single matched row, not a full ``token.save()`` (which
            # would also rewrite the hash/prefix columns unnecessarily
            # on every authenticated request).
            AnthiasAPIToken.objects.filter(pk=token.pk).update(
                last_used_at=timezone.now()
            )
            return (token.user, token)

        def authenticate_header(self, request):  # type: ignore[no-untyped-def]
            return 'Bearer'

    return {
        'DeprecatedBasicAuthentication': DeprecatedBasicAuthentication,
        'GatedSessionAuthentication': GatedSessionAuthentication,
        'AnthiasAPITokenAuthentication': AnthiasAPITokenAuthentication,
    }


_DRF_AUTH_CLASS_NAMES = frozenset(
    {
        'DeprecatedBasicAuthentication',
        'GatedSessionAuthentication',
        'AnthiasAPITokenAuthentication',
    }
)


def __getattr__(name: str) -> Any:
    """Lazy build of DRF auth classes via PEP-562 module ``__getattr__``.

    DRF resolves the dotted-string class names in
    ``REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']`` only when its
    app loads — at which point ``rest_framework`` is guaranteed to
    be importable AND Django is fully configured. Building the
    classes lazily means we never run the factory in environments
    where DRF isn't loaded:

    * Viewer image — its dep group in ``pyproject.toml`` excludes
      ``djangorestframework``, and the viewer's INSTALLED_APPS skips
      ``rest_framework`` anyway. ``__getattr__`` never fires because
      nothing in the viewer asks for these names.
    * Tooling / test bootstrap — code paths that import ``lib.auth``
      before ``django.setup()`` (or from a non-server process) only
      use the hash helpers and the regex; they never touch the auth
      classes, so the factory never runs.

    Once first accessed, the classes are cached on the module via
    ``globals().update`` so subsequent lookups skip the factory.
    Errors from the factory (``ImportError`` if DRF really isn't
    installed, ``ImproperlyConfigured`` if Django isn't ready, etc.)
    propagate to the caller — by that point the caller really did
    want a DRF class, so a hard failure is the right answer.
    """
    if name not in _DRF_AUTH_CLASS_NAMES:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    classes = _build_drf_auth_classes()
    globals().update(classes)
    return classes[name]


def _is_safe_login_next_source(request: HttpRequest) -> bool:
    """Decide whether a request's path is worth round-tripping through
    the login form's ``?next=`` parameter.

    Two classes of request must NOT propagate ``next``:

    * **Non-GET methods** (POST/PUT/PATCH/DELETE). After the operator
      signs in we issue a 302 → next, which the browser follows as a
      GET. Routes that only accept the original method then 405. The
      operator landing on a 405 right after signing in is worse than
      landing on the dashboard, so we drop ``next`` for unsafe
      methods.
    * **htmx partial endpoints**. The dashboard polls fragments such
      as ``assets_table_partial`` every 5s with ``HX-Request: true``;
      the operator's actual address bar still points at the parent
      page. Bouncing through ``?next=/_partials/asset-table/`` would
      land them on a bare fragment URL after sign-in instead of the
      page they were on.
    """
    if request.method != 'GET':
        return False
    return request.headers.get('HX-Request') is None


def _login_redirect(request: HttpRequest) -> HttpResponse:
    """Send an unauthenticated request to the login page, preserving
    the original destination via ``?next=`` when it's safe to do so."""
    from urllib.parse import urlencode

    from django.shortcuts import redirect
    from django.urls import reverse

    login_url = reverse('anthias_app:login')
    if not _is_safe_login_next_source(request):
        return redirect(login_url)
    # request.get_full_path() preserves any query string. The login
    # view validates `next` via url_has_allowed_host_and_scheme, so
    # an off-host value smuggled in here can't redirect outward.
    return redirect(
        f'{login_url}?{urlencode({"next": request.get_full_path()})}'
    )


def _extract_request(args: tuple[Any, ...]) -> HttpRequest | DRFRequest | None:
    """Scan positional call args for the request object.

    Shared by ``authorized`` and ``require_settings_access``. URL
    converters in Django and DRF are passed as kwargs by default, so
    for a function-based view ``args`` is normally ``(request,)`` and
    for a class-based view it's ``(self, request)``. But views called
    directly (unit tests, nested decorators that re-shuffle args) can
    pass extra positionals — scanning for the first HttpRequest / DRF
    Request instance is robust to both shapes (see git history on
    ``authorized`` for the ``args[-1]`` heuristic this replaced).
    """
    from django.http import HttpRequest as _HttpRequest
    from rest_framework.request import Request as _Request

    return next(
        (a for a in args if isinstance(a, (_HttpRequest, _Request))),
        None,
    )


def authorized[**P, R](
    orig: Callable[P, R],
) -> Callable[P, R | HttpResponse]:
    """Feature-flagged ``@login_required`` shim.

    * When ``settings['auth_backend']`` is empty the call passes
      through — devices left on the default un-authenticated config
      keep working without changes.
    * Otherwise the wrapped view runs only when ``request.user`` is
      authenticated (either via session or the basic-auth header
      middleware). Unauthenticated requests get a 302 to ``/login/``,
      with the original path threaded into ``?next=`` for routes
      where that's safe.

    Note on the return type: when ``R`` is DRF's ``Response`` (which is
    itself an ``HttpResponse`` subclass), mypy collapses
    ``Response | HttpResponse`` to just ``HttpResponse``, losing the
    ``Response``-specific attributes from the static type. This mirrors
    Django's own ``@login_required`` decorator and is intentional —
    at runtime the wrapped view still returns its concrete type.
    Callers that need the narrower type should cast at the call site.
    """
    from rest_framework.request import Request

    from anthias_server.settings import settings

    @wraps(orig)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R | HttpResponse:
        if not settings['auth_backend']:
            return orig(*args, **kwargs)

        request = _extract_request(args)
        if request is None:
            raise ValueError('No request object passed to decorated function')

        # DRF's Request wraps the underlying Django request; .user
        # delegates to it, so the middleware-set value is visible here.
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return orig(*args, **kwargs)

        # ``redirect()`` returns ``HttpResponseRedirect``; that
        # subclasses ``HttpResponse`` so it's compatible with the
        # decorated function's signature on both the Django and DRF
        # sides (DRF will pass an ``HttpResponse`` straight through
        # without re-rendering it).
        underlying = (
            request._request if isinstance(request, Request) else request
        )
        return _login_redirect(underlying)

    return decorated


def require_setup_complete[**P, R](
    orig: Callable[P, R],
) -> Callable[P, R | HttpResponse]:
    """Mandatory first-run gate: redirect to ``/setup/`` until an admin
    account exists.

    Unlike ``authorized``, this has no "disabled" state to bypass on —
    login is permanently mandatory from the moment the first admin
    account is created, so this decorator ignores
    ``settings['auth_backend']`` entirely and looks only at whether a
    persisted admin (``_persisted_operator()``) exists.

    HTML-app-only. Deliberately NOT applied to the DRF API
    (``api/views/*.py``) — that surface's "auth disabled = fully open"
    contract is unchanged by this feature (see the module docstring).
    Also not applied to ``anthias_app:setup`` itself (that would be an
    infinite redirect), nor to ``login`` / ``splash_page`` — both
    already existed as unguarded routes before this decorator, and
    ``splash_page`` in particular is the boot-time on-screen IP
    display a fresh device needs to keep rendering so an operator can
    actually find the device to go run setup on it.

    Stack this OUTSIDE ``@authorized`` (i.e. listed first / applied
    last) on every other app view, so "no admin yet" is checked before
    "not logged in".
    """
    from django.shortcuts import redirect
    from django.urls import reverse

    @wraps(orig)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R | HttpResponse:
        if _persisted_operator() is not None:
            return orig(*args, **kwargs)
        return redirect(reverse('anthias_app:setup'))

    return decorated


def require_settings_access[**P, R](
    orig: Callable[P, R],
) -> Callable[P, R | HttpResponse]:
    """Second gate for the Settings-family HTML views: being logged in
    (``authorized``) is not enough, ``request.user`` must also be
    allowed into Settings — an admin (``is_superuser``) always is; a
    regular user needs ``is_staff`` (repurposed here to mean "can
    access Settings", toggleable per-user by an admin).

    Stack this INSIDE ``@authorized`` (i.e. listed after it) since it
    assumes ``request.user`` has already been resolved by Django's
    ``AuthenticationMiddleware`` — it does its own
    ``is_authenticated`` check up front so it degrades safely
    (redirects, doesn't 500) if reached with an anonymous user, e.g.
    if ``authorized`` no-ops because ``auth_backend`` happens to be
    empty in a given environment.
    """
    from django.contrib import messages
    from django.shortcuts import redirect
    from django.urls import reverse
    from rest_framework.request import Request

    @wraps(orig)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R | HttpResponse:
        request = _extract_request(args)
        if request is None:
            raise ValueError('No request object passed to decorated function')

        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and (user.is_staff or user.is_superuser)
        ):
            return orig(*args, **kwargs)

        underlying = (
            request._request if isinstance(request, Request) else request
        )
        messages.error(underlying, "You don't have access to Settings.")
        return redirect(reverse('anthias_app:home'))

    return decorated


def _is_last_admin(user: User) -> bool:
    """True if ``user`` is an active admin and no other active admin
    exists.

    Used to block demoting/deleting the sole remaining admin — with
    auth permanently mandatory there is no "disabled" fallback state
    to recover into, so losing the last admin would permanently lock
    every operator out of Settings (and out of ever creating another
    admin, since that itself requires Settings access).
    """
    if not (user.is_superuser and user.is_active):
        return False
    from django.contrib.auth.models import User as UserModel

    return not (
        UserModel.objects.filter(is_superuser=True, is_active=True)
        .exclude(pk=user.pk)
        .exists()
    )


# ---------------------------------------------------------------------------
# Settings-page helpers
#
# The settings save handlers (one HTML view, one DRF view) share the
# same auth-update flow; keep it in one place so the two surfaces
# can't drift.


class AuthSettingsError(ValueError):
    """Raised by ``apply_auth_settings`` with an operator-friendly
    message. ``ValueError`` parent so existing handlers that catch
    Exception/ValueError still surface the message in the UI."""


# Operator-facing strings centralised so the HTML / DRF surfaces stay
# consistent and the linter stops complaining about the duplicates.
_ERR_INCORRECT_CURRENT = 'Incorrect current password.'
_ERR_PWD_MISMATCH = 'New passwords do not match!'

# The auth_backend feature flag accepts only these values. The DRF
# settings serializer already enforces this via a ChoiceField, but the
# HTML settings form reads ``request.POST.get('auth_backend', '')``
# raw, so a hand-crafted form could otherwise persist an unknown
# value and ``@authorized`` would start enforcing login with no
# matching User row → lockout. Validate centrally here so both write
# paths share the same gate.
_VALID_AUTH_BACKENDS = frozenset({'', 'auth_basic'})


def _operator_user(
    request: AnyRequest,
) -> User | None:
    """The User row whose credentials gate this device.

    When auth is currently enabled the calling view is gated by
    ``@authorized``, so ``request.user`` is the authenticated operator
     — return them. When auth is disabled there is no operator yet
    (initial setup) and we return None.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None
    # ``request.user`` is typed as ``Any`` via ``getattr``; narrow it so
    # the function's annotated return type holds without a generic Any
    # leak (mypy --strict-no-any-return). Anthias uses Django's stock
    # ``auth.User`` model — no custom ``AUTH_USER_MODEL`` — so the
    # cast to ``User`` is safe.
    return cast('User', user)


def _persisted_operator() -> User | None:
    """The first persisted operator-account User row, if any.

    Used by ``apply_auth_settings`` to detect the case where auth is
    *currently* disabled (so ``request.user`` is anonymous) but a
    User row exists from a previous enable→disable cycle (or from the
    0005 migration that promoted ``[auth_basic]`` credentials into the
    DB regardless of the current ``auth_backend``). Without this
    lookup, an unauthenticated LAN caller could flip ``auth_backend``
    back to ``auth_basic`` with their own username/password and lock
    out the legitimate operator.

    Mirrors the same selector ``operator_username()`` uses so both
    sites agree on which User row is "the operator."

    Also doubles as the "does at least one admin exist yet" check
    behind ``require_setup_complete`` — with auth permanently
    mandatory, a non-None result here is what lets a request past the
    mandatory first-run gate instead of being sent to ``/setup/``.
    """
    from django.contrib.auth.models import User as UserModel

    return (
        UserModel.objects.filter(is_active=True, is_superuser=True)
        .order_by('id')
        .first()
        or UserModel.objects.order_by('id').first()
    )


def _require_current_password_correct(
    current_pass_correct: bool | None,
    *,
    action: str,
) -> None:
    """Shared guard for any settings change that needs the operator
    to re-prove their current password (changing the backend,
    username, or password). Caller passes the human label of the
    action being attempted so the error message is specific."""
    if current_pass_correct is None:
        raise AuthSettingsError(
            f'Must supply current password to change {action}'
        )
    if not current_pass_correct:
        raise AuthSettingsError(_ERR_INCORRECT_CURRENT)


def _validate_password_strength(
    new_pwd: str,
    user: User | None,
) -> None:
    """Run the project's ``AUTH_PASSWORD_VALIDATORS`` against the
    proposed password and translate any rejection into
    ``AuthSettingsError`` so the HTML / DRF surfaces show an
    operator-readable message instead of leaking ``ValidationError``.

    Without this hook the validators in
    ``django_project.settings.AUTH_PASSWORD_VALIDATORS``
    (UserAttributeSimilarity, MinimumLength, CommonPassword,
    NumericPassword) would silently sit unused — ``set_password()``
    just hashes whatever you give it.
    """
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    try:
        validate_password(new_pwd, user=user)
    except ValidationError as exc:
        raise AuthSettingsError(' '.join(exc.messages)) from exc


def _check_username_available(
    new_username: str,
    *,
    exclude_pk: int | None = None,
) -> None:
    """Reject a username change/creation that would collide with
    another row before ``.save()`` raises ``IntegrityError`` on the
    unique constraint — the raw IntegrityError leaks SQL in the
    messages flash. ``exclude_pk`` is the row being renamed (omit it,
    or pass ``None``, when creating a brand new user — every existing
    username is then a collision)."""
    from django.contrib.auth.models import User as UserModel

    qs = UserModel.objects.filter(username=new_username)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise AuthSettingsError(f'Username {new_username!r} is already taken.')


def _update_existing_operator(
    operator: User,
    *,
    new_username: str,
    new_pwd: str,
    new_pwd_confirm: str,
    current_pass_correct: bool | None,
) -> None:
    """Mutate the existing operator row in response to the settings
    form. Each of username / password is independently optional —
    only changes that were actually requested validate the current
    password.

    ``apply_auth_settings`` runs on every settings save (the form
    POSTs the whole page, even unrelated fields like splash-screen
    toggles), so we track which auth fields actually changed and
    skip the DB write when none of them did. ``update_fields`` on
    the targeted save also avoids touching columns we didn't touch
    in memory.
    """
    changed_fields: list[str] = []

    if new_username and new_username != operator.get_username():
        _require_current_password_correct(
            current_pass_correct, action='username'
        )
        _check_username_available(new_username, exclude_pk=operator.pk)
        operator.username = new_username
        changed_fields.append('username')

    if new_pwd:
        _require_current_password_correct(
            current_pass_correct, action='password'
        )
        if new_pwd != new_pwd_confirm:
            raise AuthSettingsError(_ERR_PWD_MISMATCH)
        _validate_password_strength(new_pwd, operator)
        operator.set_password(new_pwd)
        changed_fields.append('password')

    if changed_fields:
        operator.save(update_fields=changed_fields)


def _create_initial_operator(
    new_username: str,
    new_pwd: str,
    new_pwd_confirm: str,
) -> None:
    """First-time enable: no User row exists yet, so both username
    and password are required and the form's confirm field must
    match."""
    from django.contrib.auth.models import User

    if not new_username:
        raise AuthSettingsError('Must provide username')
    if not new_pwd:
        raise AuthSettingsError('Must provide password')
    if new_pwd != new_pwd_confirm:
        raise AuthSettingsError(_ERR_PWD_MISMATCH)

    # Validate against AUTH_PASSWORD_VALIDATORS *before* creating the
    # User row so a rejected password doesn't leave a half-created
    # superuser behind. Pass an unsaved User instance so the
    # UserAttributeSimilarity validator can still compare the
    # password against the proposed username.
    _validate_password_strength(new_pwd, User(username=new_username))

    user, _ = User.objects.update_or_create(
        username=new_username,
        defaults={
            'is_staff': True,
            'is_superuser': True,
            'is_active': True,
        },
    )
    user.set_password(new_pwd)
    user.save()


def apply_auth_settings(
    request: AnyRequest,
    *,
    new_auth_backend: str,
    current_pwd: str,
    new_username: str,
    new_pwd: str,
    new_pwd_confirm: str,
    prev_auth_backend: str,
) -> None:
    """Validate and persist auth-related settings changes.

    Raises ``AuthSettingsError`` with an operator-friendly message
    when the input is rejected. On success, mutates the
    ``django.contrib.auth.User`` row backing the operator account.
    The caller is responsible for persisting ``auth_backend`` itself
    (we don't touch the conf file from here so a failed write of one
    setting can't half-apply auth).

    API-only as of the mandatory-setup-wizard change: the HTML
    Settings page no longer exposes an ``auth_backend`` control (login
    is permanently mandatory once the first admin exists — see
    ``anthias_server.app.views.setup``) and its self-service password
    change now goes through ``apply_self_account_changes`` instead,
    which operates on ``request.user`` rather than "the" canonical
    operator. This function is kept fully intact — backend switching,
    the re-auth challenge, initial-operator creation, all of it — only
    because ``DeviceSettingsViewV2.patch`` (the v2 API) still calls it
    with the exact same contract; changing this function's behavior
    would be a breaking change to that stable, versioned API surface.

    Parameter naming note: the form field is labelled ``password`` /
    ``current_password`` / ``password_2`` in the HTML, but Sonar's
    S6437 rule fires on any kwarg whose name contains ``password``.
    Shortening to ``pwd`` here suppresses the false positive across
    the dozens of test call sites without disabling the rule
    project-wide. The HTML form names are still mapped at the call
    site (``settings_save`` and ``DeviceSettingsViewV2.patch``).
    """
    if new_auth_backend not in _VALID_AUTH_BACKENDS:
        # Reject before any DB or conf mutation so a hand-crafted POST
        # can't persist an unknown backend value and lock the device
        # out (``@authorized`` would start enforcing login with no
        # operator User row to authenticate against).
        raise AuthSettingsError(
            f'Unknown authentication backend: {new_auth_backend!r}'
        )

    # The operator who "owns" this device is the canonical persisted
    # User (first active superuser, falling back to first User) —
    # consistent with what ``operator_username()`` and the settings-page
    # pre-fill use, so an admin who ran ``manage.py createsuperuser``
    # to create a recovery account can't accidentally end up modifying
    # the wrong row through this flow.
    #
    # If a session is active AND it doesn't match the canonical
    # operator, refuse: the caller is authenticated as somebody else
    # (e.g. the recovery superuser) and shouldn't be re-keying the
    # operator's credentials. The settings page lookups would also
    # have shown them the operator's username, not their own.
    #
    # When no canonical operator exists yet, ``operator`` is None and
    # we fall through to ``_create_initial_operator`` for first-time
    # enable.
    operator = _persisted_operator()
    session_user = _operator_user(request)
    if (
        operator is not None
        and session_user is not None
        and session_user.pk != operator.pk
    ):
        raise AuthSettingsError(
            'Only the operator account can change authentication settings.'
        )

    current_pass_correct: bool | None = None
    if current_pwd:
        current_pass_correct = bool(
            operator is not None and operator.check_password(current_pwd)
        )

    # ANY change to auth_backend that touches an existing operator
    # requires the current password. The previous version of this
    # check only looked at ``prev_auth_backend`` — i.e. it skipped
    # the challenge when re-enabling after an enable→disable cycle,
    # because ``prev_auth_backend == ''`` at that point. With a
    # persisted operator in the DB that gap meant an unauthenticated
    # caller could re-enable auth with attacker-chosen credentials.
    if new_auth_backend != prev_auth_backend and operator is not None:
        if not current_pwd:
            raise AuthSettingsError(
                'Must supply current password to change authentication method'
            )
        if not current_pass_correct:
            raise AuthSettingsError(_ERR_INCORRECT_CURRENT)

    if new_auth_backend != 'auth_basic':
        return

    if operator is not None:
        _update_existing_operator(
            operator,
            new_username=new_username,
            new_pwd=new_pwd,
            new_pwd_confirm=new_pwd_confirm,
            current_pass_correct=current_pass_correct,
        )
        return

    _create_initial_operator(
        new_username=new_username,
        new_pwd=new_pwd,
        new_pwd_confirm=new_pwd_confirm,
    )


def operator_username() -> str:
    """Best-effort username of the device's operator account.

    Used by the settings page (to pre-fill the username input) and the
    /api/v2/device-settings response. Returns an empty string when
    no User row exists yet — same shape the legacy
    ``settings['user']`` produced before the migration.
    """
    from django.contrib.auth.models import User

    operator = (
        User.objects.filter(is_active=True, is_superuser=True)
        .order_by('id')
        .first()
        or User.objects.order_by('id').first()
    )
    return operator.get_username() if operator else ''


def apply_self_account_changes(
    request: AnyRequest,
    *,
    current_pwd: str,
    new_username: str,
    new_pwd: str,
    new_pwd_confirm: str,
) -> None:
    """Let a *logged-in* user change their own username/password.

    HTML-only, self-service half of the old single-operator
    ``apply_auth_settings`` flow — but scoped to ``request.user``
    rather than "the" canonical operator, since Settings is no longer
    single-user: any authenticated user reaching the Settings page
    (admin, or staff granted access) can change their own credentials
    here, current-password required either way. This is deliberately
    a different function/path from an admin resetting *someone else's*
    password (``anthias_server.app.views.user_reset_password``), which
    has no current-password challenge by design — the admin doesn't
    know the other user's password, that's the point of a reset.

    Raises ``AuthSettingsError`` with an operator-friendly message
    when the input is rejected; both username and password are
    independently optional (only changes actually requested need the
    current password), same contract as the old
    ``_update_existing_operator``.
    """
    user = _operator_user(request)
    if user is None:
        raise AuthSettingsError(
            'You must be signed in to change your account.'
        )

    changed_fields: list[str] = []
    current_pass_correct: bool | None = None
    if current_pwd:
        current_pass_correct = user.check_password(current_pwd)

    if new_username and new_username != user.get_username():
        _require_current_password_correct(
            current_pass_correct, action='username'
        )
        _check_username_available(new_username, exclude_pk=user.pk)
        user.username = new_username
        changed_fields.append('username')

    if new_pwd:
        _require_current_password_correct(
            current_pass_correct, action='password'
        )
        if new_pwd != new_pwd_confirm:
            raise AuthSettingsError(_ERR_PWD_MISMATCH)
        _validate_password_strength(new_pwd, user)
        user.set_password(new_pwd)
        changed_fields.append('password')

    if changed_fields:
        user.save(update_fields=changed_fields)
