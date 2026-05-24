from django.conf import settings
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.users.models import CustomUser
from apps.web.middleware.locale import UserTimezoneMiddleware


class UserTimezoneMiddlewareTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = UserTimezoneMiddleware(lambda r: r)

    def tearDown(self):
        # reset timezone state between tests since activate() sets a thread-local
        timezone.deactivate()

    def _make_request(self, user):
        request = self.factory.get("/")
        request.user = user
        return request

    def test_valid_timezone_is_activated(self):
        user = CustomUser.objects.create(username="a@example.com", timezone="Europe/London")
        self.middleware(self._make_request(user))
        self.assertEqual(str(timezone.get_current_timezone()), "Europe/London")

    def test_unknown_timezone_falls_back_to_default(self):
        # first activate a non-default zone to prove the middleware actively clears it
        timezone.activate("Europe/London")
        user = CustomUser.objects.create(username="b@example.com", timezone="Not/A/Zone")
        self.middleware(self._make_request(user))
        self.assertEqual(str(timezone.get_current_timezone()), settings.TIME_ZONE)

    def test_malformed_timezone_path_falls_back_to_default(self):
        # absolute/traversal paths raise ValueError from ZoneInfo
        timezone.activate("Europe/London")
        user = CustomUser.objects.create(username="c@example.com", timezone="/etc/passwd")
        self.middleware(self._make_request(user))
        self.assertEqual(str(timezone.get_current_timezone()), settings.TIME_ZONE)

    def test_empty_timezone_uses_default(self):
        timezone.activate("Europe/London")
        user = CustomUser.objects.create(username="d@example.com", timezone="")
        self.middleware(self._make_request(user))
        self.assertEqual(str(timezone.get_current_timezone()), settings.TIME_ZONE)
