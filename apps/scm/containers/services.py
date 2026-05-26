# Container services — all business logic and write operations.
from apps.teams.models import Team

from .models import Container


def create_container(team: Team, **kwargs) -> Container:
    return Container.objects.create(team=team, **kwargs)


def update_container(container: Container, **kwargs) -> Container:
    for field, value in kwargs.items():
        setattr(container, field, value)
    container.save()
    return container


def delete_container(container: Container) -> None:
    container.delete()
