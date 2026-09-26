"""Tests for how the database connection is configured behind a transaction pooler.

``DISABLE_SERVER_SIDE_CURSORS`` is a per-connection option that Django reads from
``connections[alias].settings_dict``. As a top-level setting it does nothing, so these
tests evaluate the settings module and look inside ``DATABASES``, not at a top-level name.
"""

import os
import runpy
from pathlib import Path
from unittest import mock

from django.db import connections
from django.test import SimpleTestCase

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "container_scm" / "settings.py"

POOLED_URL = "postgres://user:pw@ep-cool-name-123456-pooler.eu-central-1.aws.neon.tech:5432/db"
DIRECT_URL = "postgres://user:pw@ep-cool-name-123456.eu-central-1.aws.neon.tech:5432/db"


def _load_settings(**environ) -> dict:
    """Evaluate the settings module with ``environ`` layered over the real environment."""
    overrides = {"SENTRY_DSN": "", **environ}
    with mock.patch.dict(os.environ, overrides):
        if "DJANGO_DATABASE_POOLED" not in environ:
            os.environ.pop("DJANGO_DATABASE_POOLED", None)
        return runpy.run_path(str(SETTINGS_PATH))


class ServerSideCursorSettingsTest(SimpleTestCase):
    def test_neon_pooler_host_disables_server_side_cursors_on_the_connection(self):
        settings = _load_settings(DATABASE_URL=POOLED_URL)

        self.assertIs(settings["DATABASES"]["default"]["DISABLE_SERVER_SIDE_CURSORS"], True)

    def test_direct_host_keeps_server_side_cursors(self):
        settings = _load_settings(DATABASE_URL=DIRECT_URL)

        self.assertIs(settings["DATABASES"]["default"]["DISABLE_SERVER_SIDE_CURSORS"], False)

    def test_env_override_forces_it_on_for_a_pooler_the_host_does_not_reveal(self):
        settings = _load_settings(DATABASE_URL=DIRECT_URL, DJANGO_DATABASE_POOLED="true")

        self.assertIs(settings["DATABASES"]["default"]["DISABLE_SERVER_SIDE_CURSORS"], True)

    def test_env_override_can_force_it_off_for_a_pooler_host(self):
        settings = _load_settings(DATABASE_URL=POOLED_URL, DJANGO_DATABASE_POOLED="false")

        self.assertIs(settings["DATABASES"]["default"]["DISABLE_SERVER_SIDE_CURSORS"], False)

    def test_it_is_not_left_as_a_top_level_setting(self):
        settings = _load_settings(DATABASE_URL=POOLED_URL)

        self.assertNotIn("DISABLE_SERVER_SIDE_CURSORS", settings)

    def test_the_running_connection_carries_the_option(self):
        # What Django's QuerySet.iterator() actually consults.
        self.assertIn("DISABLE_SERVER_SIDE_CURSORS", connections["default"].settings_dict)
