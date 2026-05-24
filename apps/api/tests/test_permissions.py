from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from apps.api.models import UserAPIKey
from apps.api.permissions import (
    HasUserAPIKey,
    IsAuthenticatedOrHasUserAPIKey,
    IsAuthenticatedOrHasUserAPIKeySuperUser,
    IsAuthenticatedSuperUser,
    IsSuperUser,
)
from apps.users.models import CustomUser


class PermissionTestCase(TestCase):
    """Base class with shared setup for permission tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user = CustomUser.objects.create(username="user@example.com")
        cls.superuser = CustomUser.objects.create(username="super@example.com", is_superuser=True)
        cls.inactive_user = CustomUser.objects.create(username="inactive@example.com", is_active=False)

        _, cls.user_api_key = UserAPIKey.objects.create_key(name="user-key", user=cls.user)
        _, cls.superuser_api_key = UserAPIKey.objects.create_key(name="super-key", user=cls.superuser)
        _, cls.inactive_api_key = UserAPIKey.objects.create_key(name="inactive-key", user=cls.inactive_user)

    def _request(self, user=None, api_key=None):
        request = self.factory.get("/fake/")
        request.user = user or AnonymousUser()
        if api_key:
            request.META["HTTP_AUTHORIZATION"] = f"Api-Key {api_key}"
        return request


class HasUserAPIKeyTest(PermissionTestCase):
    """Tests for HasUserAPIKey - API key authentication only."""

    def test_no_api_key_denied(self):
        self.assertFalse(HasUserAPIKey().has_permission(self._request(), None))

    def test_valid_api_key_allowed(self):
        self.assertTrue(HasUserAPIKey().has_permission(self._request(api_key=self.user_api_key), None))

    def test_api_key_populates_request_user(self):
        request = self._request(api_key=self.user_api_key)
        HasUserAPIKey().has_permission(request, None)
        self.assertEqual(request.user, self.user)

    def test_inactive_user_api_key_denied(self):
        self.assertFalse(HasUserAPIKey().has_permission(self._request(api_key=self.inactive_api_key), None))


class IsSuperUserTest(PermissionTestCase):
    """Tests for IsSuperUser - superuser check only."""

    def test_anonymous_denied(self):
        self.assertFalse(IsSuperUser().has_permission(self._request(), None))

    def test_regular_user_denied(self):
        self.assertFalse(IsSuperUser().has_permission(self._request(self.user), None))

    def test_superuser_allowed(self):
        self.assertTrue(IsSuperUser().has_permission(self._request(self.superuser), None))


class IsAuthenticatedOrHasUserAPIKeyTest(PermissionTestCase):
    """Tests for IsAuthenticatedOrHasUserAPIKey - session OR API key auth."""

    def test_anonymous_denied(self):
        self.assertFalse(IsAuthenticatedOrHasUserAPIKey().has_permission(self._request(), None))

    def test_session_auth_allowed(self):
        self.assertTrue(IsAuthenticatedOrHasUserAPIKey().has_permission(self._request(self.user), None))

    def test_api_key_auth_allowed(self):
        self.assertTrue(IsAuthenticatedOrHasUserAPIKey().has_permission(self._request(api_key=self.user_api_key), None))


class IsAuthenticatedSuperUserTest(PermissionTestCase):
    """Tests for IsAuthenticatedSuperUser - session auth AND superuser."""

    def test_regular_user_denied(self):
        self.assertFalse(IsAuthenticatedSuperUser().has_permission(self._request(self.user), None))

    def test_superuser_allowed(self):
        self.assertTrue(IsAuthenticatedSuperUser().has_permission(self._request(self.superuser), None))


class IsAuthenticatedOrHasUserAPIKeySuperUserTest(PermissionTestCase):
    """Tests for IsAuthenticatedOrHasUserAPIKeySuperUser - (session OR API key) AND superuser."""

    def test_regular_user_session_denied(self):
        self.assertFalse(IsAuthenticatedOrHasUserAPIKeySuperUser().has_permission(self._request(self.user), None))

    def test_superuser_session_allowed(self):
        self.assertTrue(IsAuthenticatedOrHasUserAPIKeySuperUser().has_permission(self._request(self.superuser), None))

    def test_regular_user_api_key_denied(self):
        self.assertFalse(
            IsAuthenticatedOrHasUserAPIKeySuperUser().has_permission(self._request(api_key=self.user_api_key), None)
        )

    def test_superuser_api_key_allowed(self):
        self.assertTrue(
            IsAuthenticatedOrHasUserAPIKeySuperUser().has_permission(
                self._request(api_key=self.superuser_api_key), None
            )
        )
