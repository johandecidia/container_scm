"""Tests for import Celery task safety: retry config and missing-job handling."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.scm.imports.tasks import async_parse_import_job, async_validate_import_job

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class ImportTaskRetryConfigTest(TestCase):
    def test_parse_task_has_retry_config(self):
        self.assertGreater(async_parse_import_job.max_retries, 0)

    def test_validate_task_has_retry_config(self):
        self.assertGreater(async_validate_import_job.max_retries, 0)


@override_settings(STORAGES=_TEST_STORAGES)
class ImportTaskMissingJobTest(TestCase):
    """Tasks must not raise when the job does not exist (silent skip)."""

    def test_parse_missing_job_does_not_raise(self):
        # Call the underlying function directly (bypasses Celery retry machinery)
        # A missing job should log a warning and return, not raise.
        with patch("apps.scm.imports.tasks.logger") as mock_logger:
            async_parse_import_job.run(999999)
            mock_logger.warning.assert_called_once()

    def test_validate_missing_job_does_not_raise(self):
        with patch("apps.scm.imports.tasks.logger") as mock_logger:
            async_validate_import_job.run(999999)
            mock_logger.warning.assert_called_once()
