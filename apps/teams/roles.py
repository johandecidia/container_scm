from __future__ import annotations

from django.contrib.auth.models import AnonymousUser

from apps.users.models import CustomUser

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

ROLE_CHOICES = (
    # customize roles here
    (ROLE_ADMIN, "Administrator"),
    (ROLE_MEMBER, "Member"),
)


def is_member(user: CustomUser | AnonymousUser, team) -> bool:
    if not user.is_authenticated:
        return False
    if not team:
        return False
    return team.members.filter(id=user.id).exists()


def is_admin(user: CustomUser | AnonymousUser, team) -> bool:
    if not user.is_authenticated:
        return False
    if not team:
        return False

    from .models import Membership

    return Membership.objects.filter(team=team, user=user, role=ROLE_ADMIN).exists()
