from django.test import TestCase
from rest_framework.test import APIClient

from apps.teams.models import Invitation, Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser


class ResendInvitationApiTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Test Team", slug="test-team")
        self.admin = CustomUser.objects.create_user(username="admin@test.com", email="admin@test.com", password="pass")
        self.member = CustomUser.objects.create_user(
            username="member@test.com", email="member@test.com", password="pass"
        )
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.invitation = Invitation.objects.create(
            team=self.team,
            email="invitee@test.com",
            invited_by=self.admin,
        )
        self.client = APIClient()

    def _get_resend_url(self, invitation):
        return f"/a/{self.team.slug}/team/api/invitations/{invitation.id}/resend/"

    def test_admin_can_resend_invitation(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self._get_resend_url(self.invitation))
        self.assertEqual(response.status_code, 200)

    def test_member_cannot_resend_invitation(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(self._get_resend_url(self.invitation))
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_cannot_resend(self):
        response = self.client.post(self._get_resend_url(self.invitation))
        self.assertEqual(response.status_code, 403)
