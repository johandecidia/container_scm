"""Simple health check views for liveness and dependency probing."""

import logging

from django.core.cache import cache
from django.db import OperationalError, connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def health(request):
    """Basic liveness check — returns 200 if the app process is up."""
    return JsonResponse({"status": "ok"})


def health_db(request):
    """Database connectivity check — runs a trivial query."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        logger.exception("health_db: database check failed")
        return JsonResponse({"database": "error"}, status=503)
    return JsonResponse({"database": "ok"})


def health_redis(request):
    """Redis connectivity check — verifies the default cache is reachable."""
    try:
        cache.set("_health_ping", "1", timeout=5)
        value = cache.get("_health_ping")
        if value != "1":
            raise RuntimeError("Cache ping returned unexpected value")
    except Exception:  # noqa: BLE001
        logger.exception("health_redis: redis check failed")
        return JsonResponse({"redis": "error"}, status=503)
    return JsonResponse({"redis": "ok"})
