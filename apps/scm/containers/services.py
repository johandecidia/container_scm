# Container services — all business logic and write operations.
from apps.teams.models import Team

from .models import Container


def create_container(team: Team, container_number: str, **kwargs) -> Container:
    return Container.objects.create(team=team, container_number=container_number, **kwargs)


def update_container_status(container: Container, status: str) -> Container:
    container.status = status
    container.save(update_fields=["status", "updated_at"])
    return container


def delete_container(container: Container) -> None:
    container.delete()
