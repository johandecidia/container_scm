# Container services — all business logic and write operations.
#
# Physical position is not decided here. Every service below that changes where a
# container is delegates to `movements.record_container_movement`, which owns the
# validation, the precedence rules and the projection onto
# `Container.current_location`. See movements.py.
from apps.teams.models import Team
from apps.users.models import CustomUser

from .choices import LocationSource, MovementType
from .models import Container, ContainerLocation, ContainerMovement, LocationAlias
from .movements import record_container_movement


def create_container(team: Team, user: CustomUser, data: dict) -> Container:
    """Create a new container belonging to the given team.

    A container created with a location gets a ``CREATED`` movement for it, so the
    position it starts with has the same audit trail as every one that follows. The
    location is written by the projection rather than by the insert, which is why it
    is held back and recorded afterwards.
    """
    data = dict(data)
    location = data.pop("current_location", None)
    container = Container.objects.create(
        team=team,
        created_by=user,
        updated_by=user,
        **data,
    )
    if location is not None:
        record_container_movement(
            team=team,
            container=container,
            movement_type=MovementType.CREATED,
            to_location=location,
            occurred_at=container.created_at,
            source=container.location_source or LocationSource.MANUAL,
        )
    return container


def update_container(container: Container, user: CustomUser, data: dict) -> Container:
    """Update the given container and record who made the change.

    A location change goes through the movement service rather than being written
    onto the row: an edit that moves a box is a physical movement, and recording it
    as one is what keeps the history able to explain the current position. The rest
    of the edit is an ordinary field update.
    """
    data = dict(data)
    moves = "current_location" in data
    new_location = data.pop("current_location", None)
    old_location_id = container.current_location_id

    for field, value in data.items():
        setattr(container, field, value)
    container.updated_by = user
    container.save()

    new_location_id = new_location.pk if new_location is not None else None
    if moves and new_location_id != old_location_id:
        record_container_movement(
            team=container.team,
            container=container,
            movement_type=MovementType.MANUAL_ADJUSTMENT,
            to_location=new_location,
            source=container.location_source or LocationSource.MANUAL,
        )
    return container


def set_container_location(
    container: Container,
    location: ContainerLocation | None,
    *,
    source: str = LocationSource.MANUAL,
    movement_type: str = MovementType.POSITION_UPDATE,
    occurred_at=None,
    notes: str = "",
) -> ContainerMovement:
    """Set a container's current location and record the movement.

    Kept for its callers, and now a thin call onto the state transition service. The
    behaviour it gains from that is the point: the location is set only if this
    movement is actually the container's newest accepted one, so passing a
    historical ``occurred_at`` records history instead of rewriting the present.
    """
    return record_container_movement(
        team=container.team,
        container=container,
        movement_type=movement_type,
        to_location=location,
        occurred_at=occurred_at,
        source=source,
        notes=notes,
    )


def create_location(team: Team, data: dict) -> ContainerLocation:
    """Create a new canonical location.

    ``full_clean`` runs because the hierarchy rules — no self-parenting, no cycle,
    no parent from another team — are enforced in ``clean``, and a caller that is
    not a form would otherwise bypass them.
    """
    location = ContainerLocation(team=team, **data)
    location.full_clean()
    location.save()
    return location


def update_location(location: ContainerLocation, data: dict) -> ContainerLocation:
    """Update a canonical location."""
    for field, value in data.items():
        setattr(location, field, value)
    location.full_clean()
    location.save()
    return location


def create_location_alias(team: Team, location: ContainerLocation, data: dict) -> LocationAlias:
    """Record what an external source calls *location*.

    The alias is the explicit, operator-owned decision that "GOTHENBURG" from Traqo
    means this place. Nothing infers one, and nothing creates one while reading a
    carrier response — see ``location_resolver``.
    """
    if location.team_id != team.pk:
        raise ValueError("The location belongs to a different team.")
    alias = LocationAlias(team=team, location=location, **data)
    alias.full_clean()
    alias.save()
    return alias


def delete_location_alias(team: Team, alias: LocationAlias) -> None:
    """Remove an alias.

    Hard-deleted rather than deactivated: an alias is a mapping, and a mapping that
    should no longer apply has no history worth keeping. Resolutions derived from it
    are recomputed on the next refresh of the events concerned.
    """
    if alias.team_id != team.pk:
        raise ValueError("The alias belongs to a different team.")
    alias.delete()


def delete_container(container: Container, user: CustomUser) -> None:  # noqa: ARG001
    """Hard-delete the given container."""
    container.delete()
