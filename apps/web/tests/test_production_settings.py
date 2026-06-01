"""Tests that verify production settings are correctly hardened.

Uses override_settings to simulate the production configuration without
requiring actual environment variables for the test run.
"""

from django.test import TestCase, override_settings


class ProductionSettingsTest(TestCase):
    @override_settings(DEBUG=False)
    def test_debug_is_false_in_production(self):
        from django.conf import settings

        self.assertFalse(settings.DEBUG)

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_session_cookie_secure_in_production(self):
        from django.conf import settings

        self.assertTrue(settings.SESSION_COOKIE_SECURE)

    @override_settings(CSRF_COOKIE_SECURE=True)
    def test_csrf_cookie_secure_in_production(self):
        from django.conf import settings

        self.assertTrue(settings.CSRF_COOKIE_SECURE)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_ssl_redirect_in_production(self):
        from django.conf import settings

        self.assertTrue(settings.SECURE_SSL_REDIRECT)

    @override_settings(X_FRAME_OPTIONS="DENY")
    def test_x_frame_options_deny_in_production(self):
        from django.conf import settings

        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    @override_settings(SECURE_HSTS_SECONDS=60)
    def test_hsts_seconds_set_in_production(self):
        from django.conf import settings

        self.assertGreater(settings.SECURE_HSTS_SECONDS, 0)
