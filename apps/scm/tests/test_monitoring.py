"""Tests for SCM monitoring helpers (apps/scm/monitoring.py)."""

import logging
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.scm.monitoring import (
    add_sentry_breadcrumb,
    capture_scm_exception,
    get_scm_logger,
    log_analytics_failed,
    log_carrier_api_failed,
    log_container_discovery_failed,
    log_import_completed,
    log_import_failed,
    log_import_started,
    log_tracking_sync_completed,
    log_tracking_sync_failed,
    log_tracking_sync_started,
    set_sentry_scm_context,
)


class GetScmLoggerTests(TestCase):
    def test_returns_logger_with_correct_name(self):
        logger = get_scm_logger("apps.scm.imports.services")
        self.assertIsInstance(logger, logging.Logger)
        self.assertEqual(logger.name, "apps.scm.imports.services")

    def test_logger_is_under_apps_namespace(self):
        logger = get_scm_logger("apps.scm.tracking.sync")
        self.assertTrue(logger.name.startswith("apps.scm"))


class SentryHelpersNoSentryTests(TestCase):
    """All Sentry helpers must be safe no-ops when sentry_sdk is not importable."""

    def test_set_sentry_scm_context_no_sentry(self):
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            # Should not raise
            set_sentry_scm_context(team_id=1, team_slug="acme")

    def test_add_sentry_breadcrumb_no_sentry(self):
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            add_sentry_breadcrumb("test message")

    def test_capture_scm_exception_no_sentry(self):
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            capture_scm_exception(ValueError("boom"))


class SentryHelpersWithSentryTests(TestCase):
    """Sentry helpers call the SDK when it is available."""

    def _make_scope_mock(self):
        scope = MagicMock()
        return scope

    def test_set_sentry_scm_context_sets_tags(self):
        scope_mock = self._make_scope_mock()
        sentry_mock = MagicMock()
        sentry_mock.get_current_scope.return_value = scope_mock
        with patch.dict("sys.modules", {"sentry_sdk": sentry_mock}):
            set_sentry_scm_context(team_id=42, team_slug="acme", carrier="MAEU")
        scope_mock.set_tag.assert_any_call("scm.team_id", "42")
        scope_mock.set_tag.assert_any_call("team", "acme")
        scope_mock.set_tag.assert_any_call("scm.carrier", "MAEU")

    def test_set_sentry_scm_context_skips_none_values(self):
        scope_mock = self._make_scope_mock()
        sentry_mock = MagicMock()
        sentry_mock.get_current_scope.return_value = scope_mock
        with patch.dict("sys.modules", {"sentry_sdk": sentry_mock}):
            set_sentry_scm_context(team_id=1)
        # Only team_id should be set, not the others
        calls = [c[0][0] for c in scope_mock.set_tag.call_args_list]
        self.assertIn("scm.team_id", calls)
        self.assertNotIn("team", calls)
        self.assertNotIn("scm.carrier", calls)

    def test_add_sentry_breadcrumb_calls_sdk(self):
        sentry_mock = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": sentry_mock}):
            add_sentry_breadcrumb("Import started", category="scm.import", data={"job_id": 1})
        sentry_mock.add_breadcrumb.assert_called_once_with(
            message="Import started", category="scm.import", data={"job_id": 1}
        )

    def test_sentry_helpers_survive_sdk_internal_error(self):
        sentry_mock = MagicMock()
        sentry_mock.get_current_scope.side_effect = RuntimeError("internal sentry error")
        with patch.dict("sys.modules", {"sentry_sdk": sentry_mock}):
            # Must not propagate
            set_sentry_scm_context(team_id=1)


class LogImportHelpersTests(TestCase):
    def _logger(self):
        return get_scm_logger("test.scm")

    def test_log_import_started_emits_info(self):
        logger = self._logger()
        with self.assertLogs("test.scm", level="INFO") as cm:
            log_import_started(logger, job_id=5, import_type="containers", team_id=99)
        self.assertTrue(any("scm.import.started" in line for line in cm.output))
        self.assertTrue(any("job_id=5" in line for line in cm.output))

    def test_log_import_completed_emits_info(self):
        logger = self._logger()
        with self.assertLogs("test.scm", level="INFO") as cm:
            log_import_completed(logger, job_id=5, import_type="containers", team_id=99, processed=10, failed=2)
        self.assertTrue(any("scm.import.completed" in line for line in cm.output))
        self.assertTrue(any("processed=10" in line for line in cm.output))

    def test_log_import_failed_emits_error(self):
        logger = self._logger()
        with self.assertLogs("test.scm", level="ERROR") as cm:
            log_import_failed(logger, job_id=5, import_type="containers", team_id=99, error="file parse error")
        self.assertTrue(any("scm.import.failed" in line for line in cm.output))

    def test_no_secrets_in_import_log(self):
        logger = self._logger()
        with self.assertLogs("test.scm", level="INFO") as cm:
            log_import_started(logger, job_id=1, import_type="containers", team_id=1)
        for line in cm.output:
            self.assertNotIn("password", line.lower())
            self.assertNotIn("token", line.lower())
            self.assertNotIn("secret", line.lower())
            self.assertNotIn("api_key", line.lower())


class LogTrackingHelpersTests(TestCase):
    def _logger(self):
        return get_scm_logger("test.scm.tracking")

    def test_log_tracking_sync_started(self):
        logger = self._logger()
        with self.assertLogs("test.scm.tracking", level="INFO") as cm:
            log_tracking_sync_started(logger, subscription_id=10, provider="MAEU", team_id=1)
        self.assertTrue(any("scm.tracking.sync.started" in line for line in cm.output))

    def test_log_tracking_sync_completed(self):
        logger = self._logger()
        with self.assertLogs("test.scm.tracking", level="INFO") as cm:
            log_tracking_sync_completed(
                logger, subscription_id=10, provider="MAEU", team_id=1, events_created=3, events_updated=1
            )
        self.assertTrue(any("scm.tracking.sync.completed" in line for line in cm.output))
        self.assertTrue(any("events_created=3" in line for line in cm.output))

    def test_log_tracking_sync_failed(self):
        logger = self._logger()
        with self.assertLogs("test.scm.tracking", level="ERROR") as cm:
            log_tracking_sync_failed(logger, subscription_id=10, provider="MAEU", team_id=1, error="timeout")
        self.assertTrue(any("scm.tracking.sync.failed" in line for line in cm.output))

    def test_log_carrier_api_failed(self):
        logger = self._logger()
        with self.assertLogs("test.scm.tracking", level="ERROR") as cm:
            log_carrier_api_failed(logger, carrier="MAEU", endpoint="/events", error="403 Forbidden", team_id=2)
        self.assertTrue(any("scm.carrier.api.failed" in line for line in cm.output))

    def test_log_container_discovery_failed(self):
        logger = self._logger()
        with self.assertLogs("test.scm.tracking", level="ERROR") as cm:
            log_container_discovery_failed(logger, container_number="CSQU3054187", team_id=1, error="not found")
        self.assertTrue(any("scm.container.discovery.failed" in line for line in cm.output))

    def test_log_analytics_failed(self):
        logger = self._logger()
        with self.assertLogs("test.scm.tracking", level="ERROR") as cm:
            log_analytics_failed(logger, team_id=1, error="db error")
        self.assertTrue(any("scm.analytics.failed" in line for line in cm.output))
