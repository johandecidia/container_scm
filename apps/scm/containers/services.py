# Container services — all business logic and write operations.
from apps.teams.models import Team
from apps.users.models import CustomUser

from .models import Container


def create_container(team: Team, user: CustomUser, data: dict) -> Container:
    """Create a new container belonging to the given team."""
    return Container.objects.create(
        team=team,
        created_by=user,
        updated_by=user,
        **data,
    )


def update_container(container: Container, user: CustomUser, data: dict) -> Container:
    """Update the given container and record who made the change."""
    for field, value in data.items():
        setattr(container, field, value)
    container.updated_by = user
    container.save()
    return container


def delete_container(container: Container, user: CustomUser) -> None:  # noqa: ARG001
    """Hard-delete the given container."""
    container.delete()
