#!/bin/bash

ENVIRONMENT=${ENVIRONMENT:-production}

mkdir -p "${FLEET_MEDIA_ROOT:-/data/media}"

echo "Running migration..."
python -m anthias_fleet_server.manage migrate --noinput

echo "Collecting static files..."
python -m anthias_fleet_server.manage collectstatic --noinput --clear

UVICORN_BIND_HOST="${LISTEN:-0.0.0.0}"
UVICORN_BIND_PORT="${PORT:-8080}"

if [[ "$ENVIRONMENT" == "development" ]]; then
    echo "Starting uvicorn (development, --reload)..."
    exec uvicorn anthias_fleet_server.django_project.asgi:application \
        --host "$UVICORN_BIND_HOST" \
        --port "$UVICORN_BIND_PORT" \
        --timeout-keep-alive 30 \
        --reload \
        --reload-dir /usr/src/app/src/anthias_fleet_server
else
    echo "Starting uvicorn..."
    exec uvicorn anthias_fleet_server.django_project.asgi:application \
        --host "$UVICORN_BIND_HOST" \
        --port "$UVICORN_BIND_PORT" \
        --timeout-keep-alive 30
fi
