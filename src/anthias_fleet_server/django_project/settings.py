"""Django settings for the Fleet Server — the control-plane service
described in CLAUDE.md's "Two-product direction" section.

Deliberately its own, much simpler settings module rather than a
variant of ``anthias_server.django_project.settings``: no viewer/
celery service branching, no per-device ``AnthiasSettings`` singleton,
no Redis/Channels (nothing here needs them yet — see plan phase 6 for
when that changes). What it *does* share with the Player service is
the same pinned Django/DRF/drf-spectacular versions (see pyproject.toml's
``fleet-server`` dependency group) and the same "PostgreSQL in
production, SQLite for the fast local unit-test loop" split the Player
uses for its own test suite.
"""

from os import getenv
from pathlib import Path

import django_stubs_ext

# Makes Django generic classes subscriptable at runtime — core/admin.py
# defines e.g. `class OrganizationAdmin(admin.ModelAdmin[Organization])`
# at import time, which raises TypeError without this. Unlike the
# Player's settings.py, the import isn't wrapped optional: every
# fleet-server deployment ships django-stubs-ext (see pyproject.toml's
# `fleet-server` group), there's no viewer-equivalent slim image that
# needs to exclude it.
django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent

ENVIRONMENT = getenv('ENVIRONMENT', 'production')
DEBUG = ENVIRONMENT in {'development', 'test'}

# SECURITY WARNING: keep the secret key used in production secret!
# Mirrors anthias_server's own settings.py: an explicit, obviously-
# fake fallback for local/dev use, expected to be overridden via env
# for any real deployment (the docker-compose fleet-server service
# sets FLEET_SECRET_KEY).
SECRET_KEY = getenv(
    'FLEET_SECRET_KEY',
    'django-insecure-fleet-server-dev-only-6f2ka9$3ph@x!m7wq0z',
)

# The Fleet Server is reached by its own hostname/domain (unlike a
# Player, it isn't a LAN appliance found by IP) — default to '*' for
# local/dev convenience, but operators are expected to set this via
# env for any real deployment.
ALLOWED_HOSTS = [
    h.strip() for h in getenv('ALLOWED_HOSTS', '*').split(',') if h.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'drf_spectacular',
    'anthias_fleet_server.core.apps.CoreConfig',
    'anthias_fleet_server.api.apps.ApiConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'anthias_fleet_server.django_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'anthias_fleet_server.django_project.wsgi.application'
ASGI_APPLICATION = 'anthias_fleet_server.django_project.asgi.application'

# PostgreSQL in production/dev — chosen over the Player's SQLite
# specifically because this service is multi-tenant-shaped and needs
# real concurrent-write support (see the plan's target-architecture
# section for why this is the one deliberate storage-engine deviation
# from "stay consistent with the existing stack"). Tests use SQLite —
# same split the Player's own suite already relies on
# (ENVIRONMENT=test), so the unit-test loop needs neither Docker nor a
# running Postgres; engine-specific behaviour is a Docker-based
# integration-test concern for later, mirroring how the Player
# separates the two.
if ENVIRONMENT == 'test':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / '.fleet-test.db',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': getenv('FLEET_POSTGRES_HOST', 'fleet-db'),
            'PORT': getenv('FLEET_POSTGRES_PORT', '5432'),
            'NAME': getenv('FLEET_POSTGRES_DB', 'anthias_fleet'),
            'USER': getenv('FLEET_POSTGRES_USER', 'anthias_fleet'),
            'PASSWORD': getenv('FLEET_POSTGRES_PASSWORD', 'anthias_fleet'),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation'
        '.UserAttributeSimilarityValidator'
    },
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation'
        '.CommonPasswordValidator'
    },
    {
        'NAME': 'django.contrib.auth.password_validation'
        '.NumericPasswordValidator'
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': ('whitenoise.storage.CompressedManifestStaticFilesStorage'),
    },
}

# Local disk today, behind Django's storage abstraction — swapping to
# an S3-compatible backend later (see the plan's content-distribution
# section) is a STORAGES['default'] config change, not a rewrite.
MEDIA_URL = 'media/'
# Same test/prod split as DATABASES above: the unit-test loop needs a
# writable path on any host, not the container-only /data mount.
_default_media_root = (
    str(BASE_DIR / '.fleet-test-media')
    if ENVIRONMENT == 'test'
    else '/data/media'
)
MEDIA_ROOT = Path(getenv('FLEET_MEDIA_ROOT', _default_media_root))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': (
        'rest_framework.pagination.PageNumberPagination'
    ),
    'PAGE_SIZE': 50,
    # Only 'pairing' is used today (the unauthenticated poll/ack
    # endpoints in api.pairing_views — see the plan's §17.1/§17.5:
    # these are the two most exposed endpoints in the whole service,
    # reachable before any auth exists). No view opts into throttling
    # by default (no DEFAULT_THROTTLE_CLASSES), so every other
    # endpoint is unaffected.
    'DEFAULT_THROTTLE_RATES': {
        'pairing': '30/min',
    },
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Anthias Fleet Server API',
    'VERSION': '0.1.0',
}
