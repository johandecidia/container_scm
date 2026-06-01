# Rate services — all business logic and write operations.
from apps.teams.models import Team

from .models import Rate


def create_rate(team: Team, origin: str, destination: str, amount, **kwargs) -> Rate:
    return Rate.objects.create(team=team, origin=origin, destination=destination, amount=amount, **kwargs)


def calculate_rate(origin: str, destination: str, weight_kg: float) -> dict:
    """Calculate an estimated rate. Returns a dict with amount and currency."""
    # TODO: implement rate calculation logic
    return {"amount": 0, "currency": "USD"}


def delete_rate(rate: Rate) -> None:
    rate.delete()
