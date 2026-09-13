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


def admin_count(team) -> int:
    """How many administrators a team currently has."""
    from .models import Membership

    return Membership.objects.filter(team=team, role=ROLE_ADMIN).count()


def is_final_admin(membership) -> bool:
    """True when removing or demoting this membership would leave the team with no admin.

    A team with nobody who can manage it cannot invite, re-promote or be repaired from
    the product at all, so this is the one membership change that is always refused.
    Asked as a question about a membership rather than counted at each call site, so
    "remove" and "change role" cannot disagree about what the last admin is.
    """
    return membership.role == ROLE_ADMIN and admin_count(membership.team) <= 1
