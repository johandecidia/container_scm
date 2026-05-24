from django.core.management.base import BaseCommand

from apps.subscriptions.helpers import sync_subscription_model_with_stripe
from apps.teams.models import Team


class Command(BaseCommand):
    help = "Syncs Stripe subscriptions for associated data models"

    def handle(self, **options):
        for team in Team.get_items_needing_sync():
            print(f"syncing {team} with Stripe. Last synced: {team.last_synced_with_stripe or 'never'}")
            sync_subscription_model_with_stripe(team)
