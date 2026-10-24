"""Settings is five tabs, and Imports and Integrations are two of them.

Both used to be their own sidebar entries, open to every member. They now sit in
Team Settings, on the routes they have always had, behind the same rule as the rest
of the area: `@scm_team_admin_required`. Pinned here: the tabs and their order, the
active tab on each page, the sidebar no longer offering either, and the server-side
404 for a member however they arrive.
"""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.imports.tests.helpers import make_import_job
from apps.scm.integrations.models import Integration
from apps.scm.visibility.tests.factories import TEST_STORAGES
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

# (active key, url name) in the order the tabs are shown.
TABS = [
    ("members", "team_settings:members"),
    ("tracking", "team_settings:tracking"),
    ("containers", "containers:condition_list"),
    ("integrations", "integrations:list"),
    ("imports", "imports:list"),
]


def _user(email: str) -> CustomUser:
    return CustomUser.objects.create(username=email, email=email)


def _bc_integration(team: Team) -> Integration:
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"environment": "Production", "company_id": "company-guid"},
    )


@override_settings(STORAGES=TEST_STORAGES)
class SettingsTabsFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Tabs", slug="settings-tabs")
        cls.admin = _user("admin@tabs.test")
        cls.member = _user("member@tabs.test")
        cls.team.members.add(cls.admin, through_defaults={"role": ROLE_ADMIN})
        cls.team.members.add(cls.member, through_defaults={"role": ROLE_MEMBER})
        cls.job = make_import_job(cls.team, cls.admin)
        cls.integration = _bc_integration(cls.team)

        cls.other_team = Team.objects.create(name="Other", slug="settings-tabs-other")
        cls.other_team.members.add(cls.admin, through_defaults={"role": ROLE_ADMIN})
        cls.other_job = make_import_job(cls.other_team, cls.admin)
        cls.other_integration = _bc_integration(cls.other_team)

    def client_for(self, user: CustomUser, team: Team | None = None) -> Client:
        client = Client()
        client.force_login(user)
        session = client.session
        session["team"] = (team or self.team).pk
        session.save()
        return client


class SettingsTabsTest(SettingsTabsFixture):
    def test_an_admin_sees_all_five_tabs_in_order(self):
        html = self.client_for(self.admin).get(reverse("team_settings:members")).content.decode()
        positions = [html.find(f'role="tab" href="{reverse(name)}"') for _, name in TABS]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_each_tab_is_active_on_its_own_page_and_only_there(self):
        client = self.client_for(self.admin)
        pages = {
            "members": reverse("team_settings:members"),
            "tracking": reverse("team_settings:tracking"),
            "containers": reverse("containers:condition_list"),
            "integrations": reverse("integrations:detail", args=[self.integration.pk]),
            "imports": reverse("imports:detail", args=[self.job.pk]),
        }
        for active, url in pages.items():
            with self.subTest(page=url):
                html = client.get(url).content.decode()
                self.assertEqual(html.count("tab-active"), 1)
                active_url = reverse(dict(TABS)[active])
                self.assertRegex(html, rf'href="{active_url}"\s+class="tab tab-active"')

    def test_the_sidebar_marks_team_settings_active_on_imports_and_integrations(self):
        client = self.client_for(self.admin)
        settings_link = f'href="{reverse("team_settings:members")}" class="menu-active"'
        for url in (reverse("imports:list"), reverse("imports:upload"), reverse("integrations:list")):
            with self.subTest(url=url):
                self.assertIn(settings_link, client.get(url).content.decode())


class SidebarTest(SettingsTabsFixture):
    def test_imports_and_integrations_are_no_longer_sidebar_entries(self):
        for user in (self.admin, self.member):
            with self.subTest(user=user.email):
                html = self.client_for(user).get(reverse("visibility:overview")).content.decode()
                self.assertNotIn(reverse("imports:list"), html)
                self.assertNotIn(reverse("integrations:list"), html)


class SettingsAccessTest(SettingsTabsFixture):
    def _routes(self) -> list[tuple[str, str]]:
        job, integration = self.job.pk, self.integration.pk
        return [
            ("get", reverse("imports:list")),
            ("get", reverse("imports:upload")),
            ("post", reverse("imports:upload")),
            ("get", reverse("imports:detail", args=[job])),
            ("post", reverse("imports:parse", args=[job])),
            ("post", reverse("imports:validate", args=[job])),
            ("post", reverse("imports:confirm", args=[job])),
            ("get", reverse("integrations:list")),
            ("get", reverse("integrations:detail", args=[integration])),
            ("post", reverse("integrations:sync_now", args=[integration])),
            ("post", reverse("integrations:test_connection", args=[integration])),
        ]

    def test_a_member_gets_a_404_on_every_imports_and_integrations_route(self):
        client = self.client_for(self.member)
        for method, url in self._routes():
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(client, method)(url).status_code, 404)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        for url in (reverse("imports:list"), reverse("integrations:list")):
            with self.subTest(url=url):
                response = Client().get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("account_login"), response["Location"])

    def test_the_existing_urls_still_answer_for_an_admin(self):
        client = self.client_for(self.admin)
        for url in (
            reverse("imports:list"),
            reverse("imports:upload"),
            reverse("imports:detail", args=[self.job.pk]),
            reverse("integrations:list"),
            reverse("integrations:detail", args=[self.integration.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_an_admin_cannot_reach_another_teams_imports_or_integrations(self):
        """Admin of both teams, active in one: the other team's rows are a 404."""
        client = self.client_for(self.admin)
        for url in (
            reverse("imports:detail", args=[self.other_job.pk]),
            reverse("integrations:detail", args=[self.other_integration.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)

    def test_an_admin_of_another_team_only_is_refused(self):
        outsider = _user("outsider@tabs.test")
        self.other_team.members.add(outsider, through_defaults={"role": ROLE_ADMIN})
        client = self.client_for(outsider, self.other_team)
        self.assertEqual(client.get(reverse("imports:detail", args=[self.job.pk])).status_code, 404)
        self.assertEqual(client.get(reverse("integrations:detail", args=[self.integration.pk])).status_code, 404)


class OperationalLinksTest(SettingsTabsFixture):
    """Pages every member uses only offer the way into Imports to administrators."""

    def test_purchase_orders_offers_new_import_to_admins_only(self):
        upload = reverse("imports:upload")
        url = reverse("procurement:purchase_order_list")
        self.assertIn(upload, self.client_for(self.admin).get(url).content.decode())
        self.assertNotIn(upload, self.client_for(self.member).get(url).content.decode())

    def test_analytics_links_into_imports_for_admins_only(self):
        url = reverse("analytics:dashboard")
        self.assertIn(reverse("imports:list"), self.client_for(self.admin).get(url).content.decode())
        self.assertNotIn("/scm/imports/", self.client_for(self.member).get(url).content.decode())
