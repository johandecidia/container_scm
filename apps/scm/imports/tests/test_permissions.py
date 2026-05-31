"""Tests for import permission enforcement."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.imports.models import ImportJob

from .helpers import make_import_job, make_team, make_user

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=_TEST_STORAGES)
class ImportPermissionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="perm-team")
        cls.other_team = make_team(name="Other Team", slug="perm-other-team")

        cls.user = make_user("perm@example.com")
        cls.team.members.add(cls.user)

        cls.other_user = make_user("other@example.com")
        cls.other_team.members.add(cls.other_user)

        cls.job = make_import_job(cls.team, cls.user)
        cls.other_job = make_import_job(cls.other_team, cls.other_user)

    def _client(self, user):
        c = Client()
        c.force_login(user)
        session = c.session
        session["team_id"] = self.team.pk
        session.save()
        return c

    def test_unauthenticated_redirected(self):
        c = Client()
        resp = c.get(reverse("imports:list"))
        self.assertIn(resp.status_code, [302, 301])

    def test_user_cannot_access_other_team_import(self):
        c = self._client(self.user)
        resp = c.get(reverse("imports:detail", kwargs={"pk": self.other_job.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_confirm_requires_validated_status(self):
        job = make_import_job(self.team, self.user)
        # job is UPLOADED, not VALIDATED — confirm should not complete it
        c = self._client(self.user)
        c.post(reverse("imports:confirm", kwargs={"pk": job.pk}))
        job.refresh_from_db()
        self.assertNotEqual(job.status, ImportJob.Status.COMPLETED)
