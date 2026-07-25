"""Tests for the Business Central sync lock (cache + DB advisory).

The lock implementation is shared (apps.scm.integrations.locks); these tests
verify that Business Central's wrapper keeps the same behaviour and reports a
conflict as BusinessCentralSyncInProgressError.
"""

from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings

from apps.scm.integrations import locks as shared_locks
from apps.scm.integrations.business_systems.business_central import locks
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralSyncInProgressError,
)

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-lock"}}


def _advisory_count(keys):
    k1, k2 = keys
    # pg_locks exposes the two-int advisory key as (classid, objid).
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
            [k1, k2],
        )
        return cur.fetchone()[0]


@override_settings(CACHES=_LOCMEM)
class SyncLockTest(TestCase):
    def test_advisory_lock_held_for_whole_block_and_released(self):
        name = "lock-held"
        keys = shared_locks._advisory_keys(name)
        self.assertEqual(_advisory_count(keys), 0)
        with locks.sync_lock(name):
            # No TTL — held for the entire block, so a long run cannot lose it.
            self.assertEqual(_advisory_count(keys), 1)
        self.assertEqual(_advisory_count(keys), 0)

    def test_configurable_ttl_passed_to_cache(self):
        with (
            mock.patch.object(shared_locks.cache, "add", return_value=True) as add,
            locks.sync_lock("ttl-name", ttl=1234),
        ):
            pass
        self.assertEqual(add.call_args.args[2], 1234)

    def test_already_held_in_cache_raises(self):
        name = "held-in-cache"
        shared_locks.cache.add(locks.cache_lock_key(name), "someone-else", 600)
        with self.assertRaises(BusinessCentralSyncInProgressError), locks.sync_lock(name):
            pass

    def test_different_names_run_in_parallel(self):
        # A different lock name is independent and acquires fine.
        with locks.sync_lock("name-a"), locks.sync_lock("name-b"):
            pass

    def test_cache_outage_falls_back_to_advisory(self):
        with (
            mock.patch.object(shared_locks.cache, "add", side_effect=RuntimeError("down")),
            locks.sync_lock("cache-down"),
        ):
            pass  # advisory lock carried it — no exception

    def test_cache_outage_and_advisory_unavailable_raises(self):
        with (
            mock.patch.object(shared_locks.cache, "add", side_effect=RuntimeError("down")),
            mock.patch.object(shared_locks, "_try_advisory_lock", return_value=False),
            self.assertRaises(BusinessCentralSyncInProgressError),
            locks.sync_lock("both-down"),
        ):
            pass

    def test_bc_lock_key_keeps_its_own_namespace(self):
        """BC locks must not collide with tracking locks that share a name."""
        self.assertEqual(locks.cache_lock_key("x"), "bc_sync_lock:x")
