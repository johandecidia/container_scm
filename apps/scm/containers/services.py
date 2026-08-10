# Container services — all business logic and write operations.
from django.utils import timezone

from apps.teams.models import Team
from apps.users.models import CustomUser

from .choices import LocationSource, MovementType
from .models import Container, ContainerLocation, ContainerMovement


def create_container(team: Team, user: CustomUser, data: dict) -> Container:
    """Create a new container belonging to the given team."""
    container = Container.objects.create(
        team=team,
        created_by=user,
        updated_by=user,
        **data,
    )
    if container.current_location_id:
        ContainerMovement.objects.create(
            team=team,
            container=container,
            from_location=None,
            to_location=container.current_location,
            movement_type=MovementType.CREATED,
            occurred_at=container.created_at or timezone.now(),
            source=container.location_source or LocationSource.MANUAL,
        )
    return container


def update_container(container: Container, user: CustomUser, data: dict) -> Container:
    """Update the given container and record who made the change.

    Creates a ContainerMovement when current_location changes.
    """
    old_location_id = container.current_location_id
    for field, value in data.items():
        setattr(container, field, value)
    container.updated_by = user
    container.save()

    new_location_id = container.current_location_id
    if new_location_id != old_location_id:
        ContainerMovement.objects.create(
            team=container.team,
            container=container,
            from_location_id=old_location_id,
            to_location_id=new_location_id,
            movement_type=MovementType.POSITION_UPDATE,
            occurred_at=timezone.now(),
            source=container.location_source or LocationSource.MANUAL,
        )
        container.last_location_update = timezone.now()
        container.save(update_fields=["last_location_update"])

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
    """Set a container's current location and record the movement."""
    old_location = container.current_location
    container.current_location = location
    container.location_source = source
    container.last_location_update = occurred_at or timezone.now()
    container.save(update_fields=["current_location", "location_source", "last_location_update"])

    return ContainerMovement.objects.create(
        team=container.team,
        container=container,
        from_location=old_location,
        to_location=location,
        movement_type=movement_type,
        occurred_at=occurred_at or timezone.now(),
        source=source,
        notes=notes,
    )


def create_location(team: Team, data: dict) -> ContainerLocation:
    """Create a new container location."""
    return ContainerLocation.objects.create(team=team, **data)


def update_location(location: ContainerLocation, data: dict) -> ContainerLocation:
    """Update a container location."""
    for field, value in data.items():
        setattr(location, field, value)
    location.save()
    return location


def delete_container(container: Container, user: CustomUser) -> None:  # noqa: ARG001
    """Hard-delete the given container."""
    container.delete()
