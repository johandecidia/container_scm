"""Settings → Members: who may open it, and the one change that is always refused.

Two things are pinned here. The first is that the area is closed server-side: a
member without admin rights gets a 404 on every route, whether or not the
navigation ever offered them the link. The second is that a team cannot lock
itself out — the final administrator can be neither removed nor demoted, by
anybody, including themselves.
"""

from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse

from apps.teams.models import Invitation, Membership, Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser


def _user(email: str) -> CustomUser:
    return CustomUser.objects.create(username=email, email=email)


class SettingsAccessTest(TestCase):
    """Only administrators of the active team reach Settings."""

    team: Team
    admin: CustomUser
    member: CustomUser
    member_membership: Membership

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="MCR", slug="mcr-access")
        cls.admin = _user("admin@mcr.test")
        cls.member = _user("member@mcr.test")
        cls.team.members.add(cls.admin, through_defaults={"role": ROLE_ADMIN})
        cls.team.members.add(cls.member, through_defaults={"role": ROLE_MEMBER})
        cls.member_membership = Membership.objects.get(team=cls.team, user=cls.member)

    def _routes(self) -> list[tuple[str, str]]:
        """(method, url) for every Settings route, so none can be left unguarded."""
        return [
            ("get", reverse("team_settings:home")),
            ("get", reverse("team_settings:members")),
            ("post", reverse("team_settings:member_role_update", args=[self.member_membership.pk])),
            ("post", reverse("team_settings:member_remove", args=[self.member_membership.pk])),
            ("post", reverse("team_settings:invitation_send")),
        ]

    def test_a_member_without_admin_rights_gets_a_404_everywhere(self):
        client = Client()
        client.force_login(self.member)
        for method, url in self._routes():
            with self.subTest(url=url):
                response = getattr(client, method)(url)
                self.assertEqual(response.status_code, 404, url)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        response = Client().get(reverse("team_settings:members"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response["Location"])

    def test_an_admin_sees_the_members_page(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get(reverse("team_settings:members"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "admin@mcr.test")
        self.assertContains(response, "member@mcr.test")


class TeamIsolationTest(TestCase):
    """A membership of another team is not reachable from this team's Settings."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Ours", slug="ours")
        cls.other_team = Team.objects.create(name="Theirs", slug="theirs")
        cls.admin = _user("ours-admin@mcr.test")
        cls.other_admin = _user("theirs-admin@mcr.test")
        cls.other_member = _user("theirs-member@mcr.test")
        cls.team.members.add(cls.admin, through_defaults={"role": ROLE_ADMIN})
        cls.other_team.members.add(cls.other_admin, through_defaults={"role": ROLE_ADMIN})
        cls.other_team.members.add(cls.other_member, through_defaults={"role": ROLE_MEMBER})
        cls.foreign_membership = Membership.objects.get(team=cls.other_team, user=cls.other_member)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)

    def test_the_page_lists_only_this_teams_members(self):
        response = self.client.get(reverse("team_settings:members"))
        self.assertContains(response, "ours-admin@mcr.test")
        self.assertNotContains(response, "theirs-member@mcr.test")

    def test_another_teams_membership_cannot_be_removed(self):
        response = self.client.post(reverse("team_settings:member_remove", args=[self.foreign_membership.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Membership.objects.filter(pk=self.foreign_membership.pk).exists())

    def test_another_teams_membership_cannot_be_promoted(self):
        response = self.client.post(
            reverse("team_settings:member_role_update", args=[self.foreign_membership.pk]),
            {"role": ROLE_ADMIN},
        )
        self.assertEqual(response.status_code, 404)
        self.foreign_membership.refresh_from_db()
        self.assertEqual(self.foreign_membership.role, ROLE_MEMBER)

    def test_another_teams_invitation_cannot_be_cancelled(self):
        invitation = Invitation.objects.create(
            team=self.other_team, email="invitee@mcr.test", invited_by=self.other_admin
        )
        response = self.client.post(reverse("team_settings:invitation_cancel", args=[invitation.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Invitation.objects.filter(pk=invitation.pk).exists())


class MemberManagementTest(TestCase):
    """An admin can promote, demote, remove and invite."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-manage")
        self.admin = _user("boss@mcr.test")
        self.second_admin = _user("deputy@mcr.test")
        self.member = _user("ops@mcr.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.second_admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.client = Client()
        self.client.force_login(self.admin)

    def _membership(self, user) -> Membership:
        return Membership.objects.get(team=self.team, user=user)

    def test_a_member_can_be_promoted_to_admin(self):
        membership = self._membership(self.member)
        response = self.client.post(
            reverse("team_settings:member_role_update", args=[membership.pk]), {"role": ROLE_ADMIN}
        )
        self.assertEqual(response.status_code, 302)
        membership.refresh_from_db()
        self.assertEqual(membership.role, ROLE_ADMIN)

    def test_an_admin_can_be_demoted_while_another_admin_remains(self):
        membership = self._membership(self.second_admin)
        self.client.post(reverse("team_settings:member_role_update", args=[membership.pk]), {"role": ROLE_MEMBER})
        membership.refresh_from_db()
        self.assertEqual(membership.role, ROLE_MEMBER)

    def test_a_member_can_be_removed(self):
        membership = self._membership(self.member)
        self.client.post(reverse("team_settings:member_remove", args=[membership.pk]))
        self.assertFalse(Membership.objects.filter(pk=membership.pk).exists())

    def test_inviting_a_member_creates_an_invitation_and_sends_one_email(self):
        response = self.client.post(
            reverse("team_settings:invitation_send"), {"email": "new@mcr.test", "role": ROLE_MEMBER}
        )
        self.assertEqual(response.status_code, 302)
        invitation = Invitation.objects.get(team=self.team, email="new@mcr.test")
        self.assertEqual(invitation.invited_by, self.admin)
        self.assertFalse(invitation.is_accepted)
        self.assertEqual(len(mail.outbox), 1)

    def test_inviting_an_existing_member_is_rejected(self):
        self.client.post(reverse("team_settings:invitation_send"), {"email": "ops@mcr.test", "role": ROLE_MEMBER})
        self.assertFalse(Invitation.objects.filter(team=self.team, email="ops@mcr.test").exists())

    def test_an_invitation_can_be_cancelled(self):
        invitation = Invitation.objects.create(team=self.team, email="gone@mcr.test", invited_by=self.admin)
        self.client.post(reverse("team_settings:invitation_cancel", args=[invitation.id]))
        self.assertFalse(Invitation.objects.filter(pk=invitation.pk).exists())


class FinalAdminTest(TestCase):
    """The last administrator is not removable and not demotable — by anybody."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-final")
        self.admin = _user("only@mcr.test")
        self.member = _user("staff@mcr.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.admin_membership = Membership.objects.get(team=self.team, user=self.admin)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_the_final_admin_cannot_demote_themselves(self):
        self.client.post(
            reverse("team_settings:member_role_update", args=[self.admin_membership.pk]), {"role": ROLE_MEMBER}
        )
        self.admin_membership.refresh_from_db()
        self.assertEqual(self.admin_membership.role, ROLE_ADMIN)

    def test_the_final_admin_cannot_remove_themselves(self):
        self.client.post(reverse("team_settings:member_remove", args=[self.admin_membership.pk]))
        self.assertTrue(Membership.objects.filter(pk=self.admin_membership.pk).exists())

    def test_demotion_becomes_possible_once_a_second_admin_exists(self):
        other = Membership.objects.get(team=self.team, user=self.member)
        self.client.post(reverse("team_settings:member_role_update", args=[other.pk]), {"role": ROLE_ADMIN})
        self.client.post(
            reverse("team_settings:member_role_update", args=[self.admin_membership.pk]), {"role": ROLE_MEMBER}
        )
        self.admin_membership.refresh_from_db()
        self.assertEqual(self.admin_membership.role, ROLE_MEMBER)

    def test_the_only_admins_row_offers_no_remove_button(self):
        response = self.client.get(reverse("team_settings:members"))
        self.assertNotContains(response, reverse("team_settings:member_remove", args=[self.admin_membership.pk]))
        self.assertContains(response, "Only administrator")
