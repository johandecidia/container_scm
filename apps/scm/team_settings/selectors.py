# Team Settings selectors — read/query operations only.
from dataclasses import dataclass

from django.db.models import QuerySet

from apps.teams.models import Invitation, Membership, Team
from apps.teams.roles import ROLE_ADMIN


@dataclass(frozen=True)
class MemberRow:
    """One row of the members table, with the two facts the row's controls depend on.

    ``is_final_admin`` is computed once from a count the page already needed, rather
    than per row: the table is small, but a query per member for the same number is
    the kind of thing that quietly becomes a hundred queries. The view asks
    :func:`apps.teams.roles.is_final_admin` again before it writes, so this value only
    ever decides what is rendered.
    """

    membership: Membership
    is_final_admin: bool
    is_self: bool


def get_team_memberships(team: Team) -> QuerySet[Membership]:
    """The team's memberships, admins first then by email.

    Admins first because the page answers "who can manage this team", and
    ``select_related`` because every row renders a display name and an email.
    """
    return (
        Membership.objects.filter(team=team)
        .select_related("user")
        .order_by("role", "user__email")  # "admin" sorts before "member"
    )


def get_admin_count(team: Team) -> int:
    return Membership.objects.filter(team=team, role=ROLE_ADMIN).count()


def get_member_rows(team: Team, user) -> list[MemberRow]:
    """The members table, ready to render."""
    admins = get_admin_count(team)
    return [
        MemberRow(
            membership=membership,
            is_final_admin=membership.role == ROLE_ADMIN and admins <= 1,
            is_self=membership.user_id == user.pk,
        )
        for membership in get_team_memberships(team)
    ]


def get_pending_invitations(team: Team) -> QuerySet[Invitation]:
    return Invitation.objects.filter(team=team, is_accepted=False).order_by("-created_at")
