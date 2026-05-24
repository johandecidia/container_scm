import contextlib
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

import sentry_sdk

if TYPE_CHECKING:
    from apps.teams.models import Team

_context: ContextVar[Team | None] = ContextVar("team", default=None)


class EmptyTeamContextException(Exception):
    pass


def get_current_team() -> Team | None:
    """
    Util to get the team that has been set in the current thread/context using `set_current_team`.

    Will return None if the team is not set
    """
    with contextlib.suppress(LookupError):
        return _context.get()
    return None


def set_current_team(team: Team | None) -> Token:
    """
    Utils to set a team in the current thread/context.
    Used in a middleware once a user is logged in.
    """
    team = _unwrap_lazy(team)
    token = _context.set(team)
    if team and hasattr(team, "slug"):
        sentry_sdk.get_current_scope().set_tag("team", team.slug)
    else:
        sentry_sdk.get_current_scope().remove_tag("team")
    return token


def unset_current_team(token: Token | None = None):
    """
    When the token that the context was set to is passed, we use that to reset the context to its previous value,
    otherwise we set it to None.
    """
    if token is None:
        _context.set(None)
        sentry_sdk.get_current_scope().remove_tag("team")
    else:
        _context.reset(token)
        if (team := get_current_team()) and hasattr(team, "slug"):
            sentry_sdk.get_current_scope().set_tag("team", team.slug)
        else:
            sentry_sdk.get_current_scope().remove_tag("team")


@contextmanager
def current_team(team: Team | None):
    """Context manager used for setting the team outside requests where the team is set automatically."""
    token = set_current_team(team)
    try:
        yield
    finally:
        unset_current_team(token)


def _unwrap_lazy(obj):
    """Unwraps a lazy object if it is one, otherwise returns the object itself."""
    from django.utils.functional import LazyObject, empty

    if isinstance(obj, LazyObject):
        if obj._wrapped is empty:
            obj._setup()
        return obj._wrapped
    return obj
