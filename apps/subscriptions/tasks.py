from celery import shared_task
from stripe import StripeError

from apps.teams.models import Team

from .helpers import sync_subscription_model_with_stripe


@shared_task
def sync_subscriptions_task():
    for team in Team.get_items_needing_sync():
        try:
            sync_subscription_model_with_stripe(team)
        except StripeError as e:
            from sentry_sdk import capture_exception

            capture_exception(e)
