import typing
from typing import cast

from django.http import HttpRequest
from rest_framework.permissions import BasePermission, IsAuthenticated, OperandHolder
from rest_framework_api_key.permissions import BaseHasAPIKey

from .helpers import get_user_from_request
from .models import UserAPIKey


class HasUserAPIKey(BaseHasAPIKey):
    model = UserAPIKey

    def has_permission(self, request: HttpRequest, view: typing.Any) -> bool:
        has_perm = super().has_permission(request, view)
        if has_perm:
            # if they have permission, also populate the request.user object for convenience
            request.user = get_user_from_request(request)  # type: ignore[assignment]

            if request.user and not request.user.is_active:
                has_perm = False

        return has_perm


class IsSuperUser(BasePermission):
    """
    Permission class that checks if the user is a superuser.

    Use with & to combine with authentication permissions, e.g.:
        permission_classes = [IsAuthenticatedOrHasUserAPIKey & IsSuperUser]
    """

    def has_permission(self, request: HttpRequest, view: typing.Any) -> bool:
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


# hybrid permission class that can check for API keys or authentication
IsAuthenticatedOrHasUserAPIKey = cast(OperandHolder, IsAuthenticated | HasUserAPIKey)

# superuser permission combinations
IsAuthenticatedSuperUser = cast(OperandHolder, IsAuthenticated & IsSuperUser)
IsAuthenticatedOrHasUserAPIKeySuperUser = cast(OperandHolder, IsAuthenticatedOrHasUserAPIKey & IsSuperUser)
