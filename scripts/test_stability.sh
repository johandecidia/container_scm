#!/usr/bin/env bash
set -e

SERVICE_NAME="web"

echo "=== Running Django system check ==="
docker compose run --rm "$SERVICE_NAME" python manage.py check

echo "=== Checking for missing migrations ==="
docker compose run --rm "$SERVICE_NAME" python manage.py makemigrations --check --dry-run

echo "=== Running test suite ==="
docker compose run --rm "$SERVICE_NAME" python manage.py test

echo "=== Stability test completed successfully ==="
