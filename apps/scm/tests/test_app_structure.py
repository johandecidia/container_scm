"""
Kort 1 — App-struktur
Acceptanskriterier: SCM-apparna är registrerade och routing fungerar.
"""

from django.apps import apps
from django.test import SimpleTestCase
from django.urls import reverse


class ScmAppsInstalledTest(SimpleTestCase):
    def test_scm_apps_are_installed(self):
        self.assertTrue(apps.is_installed("apps.scm.containers"))
        self.assertTrue(apps.is_installed("apps.scm.shipments"))
        self.assertTrue(apps.is_installed("apps.scm.rates"))
        self.assertTrue(apps.is_installed("apps.scm.imports"))
        self.assertTrue(apps.is_installed("apps.scm.integrations"))
        self.assertTrue(apps.is_installed("apps.scm.analytics"))

    def test_scm_app_labels(self):
        self.assertEqual(apps.get_app_config("scm_containers").name, "apps.scm.containers")

    def test_container_list_url_exists(self):
        self.assertEqual(reverse("containers:list"), "/scm/containers/")

    def test_container_create_url_exists(self):
        self.assertEqual(reverse("containers:create"), "/scm/containers/create/")

    def test_container_detail_url_exists(self):
        self.assertEqual(reverse("containers:detail", kwargs={"container_id": 1}), "/scm/containers/1/")

    def test_container_update_url_exists(self):
        self.assertEqual(reverse("containers:update", kwargs={"container_id": 1}), "/scm/containers/1/edit/")
