"""Robust locking for Business Central syncs.

A sync must never run unprotected. Two locks are combined:

  1. A cache lock (fast, cross-node) acquired with ``cache.add`` and a
     configurable TTL. This is the quick guard and the first thing another node
     sees.
  2. A PostgreSQL *session* advisory lock (``pg_try_advisory_lock``) held for the
     entire run. It has no TTL, so a long initial sync can never lose it, and it
     is released automatically if the process/connection dies — so a crash never
     leaves a permanent stale lock.

If the cache is unavailable we fall back to the advisory lock alone; if neither
a safe lock can be taken, we raise instead of proceeding unprotected. The same
lock name (integration + resource type) blocks; different names run in parallel.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import contextmanager

from django.core.cache import cache
from django.db import connection

from .exceptions import BusinessCentralSyncInProgressError

logger = logging.getLogger(__name__)

# Generous default so a large initial sync cannot outlive the cache lock; the
# advisory lock (no TTL) is the real protection for long runs regardless.
DEFAULT_LOCK_TTL_SECONDS = 3600


def purchase_order_lock_name(integration) -> str:
    return f"{integration.pk}:purchase_orders"


def cache_lock_key(name: str) -> str:
    return f"bc_sync_lock:{name}"


def _advisory_keys(name: str) -> tuple[int, int]:
    """Map a lock name to the two signed 32-bit ints pg_advisory_lock expects."""
    digest = hashlib.sha256(name.encode()).digest()
    k1 = int.from_bytes(digest[0:4], "big", signed=True)
    k2 = int.from_bytes(digest[4:8], "big", signed=True)
    return k1, k2


def _try_advisory_lock(keys: tuple[int, int]) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s)", list(keys))
        return bool(cur.fetchone()[0])


def _advisory_unlock(keys: tuple[int, int]) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s, %s)", list(keys))


def _safe_cache_delete(key: str, token: str) -> None:
    try:
        if cache.get(key) == token:
            cache.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to release sync cache lock: %s", type(exc).__name__)


@contextmanager
def sync_lock(name: str, *, ttl: int = DEFAULT_LOCK_TTL_SECONDS):
    """Hold a robust lock for ``name`` for the duration of the ``with`` block.

    Raises BusinessCentralSyncInProgressError if the lock is already held (or no
    safe lock can be acquired). Both locks are released on exit.
    """
    key = cache_lock_key(name)
    token = uuid.uuid4().hex
    cache_available = True
    cache_held = False

    try:
        cache_held = bool(cache.add(key, token, ttl))
    except Exception as exc:  # noqa: BLE001 — degrade to the advisory lock, never fail-open
        cache_available = False
        logger.warning("Sync cache lock unavailable (%s); relying on DB advisory lock", type(exc).__name__)

    # Cache says it is already running elsewhere — fast rejection.
    if cache_available and not cache_held:
        raise BusinessCentralSyncInProgressError(f"A sync is already running for {name}")

    advisory_keys = _advisory_keys(name)
    advisory_held = False
    try:
        advisory_held = _try_advisory_lock(advisory_keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not acquire DB advisory lock (%s)", type(exc).__name__)
        advisory_held = False

    if not advisory_held:
        if cache_held:
            _safe_cache_delete(key, token)
        raise BusinessCentralSyncInProgressError(f"A sync is already running for {name}")

    try:
        yield
    finally:
        try:
            _advisory_unlock(advisory_keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to release DB advisory lock: %s", type(exc).__name__)
        if cache_held:
            _safe_cache_delete(key, token)
