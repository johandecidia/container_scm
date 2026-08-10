"""Business Central sync locking.

The lock itself (cache + PostgreSQL advisory lock) lives in
``apps.scm.integrations.locks`` and is shared with the carrier tracking sync.
This module only names Business Central's locks and translates a failed
acquisition into the Business Central exception type.
"""

from __future__ import annotations

from contextlib import contextmanager

from apps.scm.integrations.locks import (
    DEFAULT_LOCK_TTL_SECONDS,
    LockNotAcquiredError,
    resource_lock,
)
from apps.scm.integrations.locks import cache_lock_key as _cache_lock_key

from .exceptions import BusinessCentralSyncInProgressError

_LOCK_PREFIX = "bc_sync_lock"


def purchase_order_lock_name(integration) -> str:
    return f"{integration.pk}:purchase_orders"


def cache_lock_key(name: str) -> str:
    return _cache_lock_key(name, prefix=_LOCK_PREFIX)


@contextmanager
def sync_lock(name: str, *, ttl: int = DEFAULT_LOCK_TTL_SECONDS):
    """Hold the Business Central sync lock for ``name``.

    Raises BusinessCentralSyncInProgressError if the lock is already held (or no
    safe lock can be acquired).
    """
    try:
        with resource_lock(name, ttl=ttl, prefix=_LOCK_PREFIX):
            yield
    except LockNotAcquiredError as exc:
        raise BusinessCentralSyncInProgressError(str(exc)) from exc
