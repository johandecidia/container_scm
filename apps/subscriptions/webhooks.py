import logging

from django.core.mail import mail_admins
from djstripe.event_handlers import djstripe_receiver
from djstripe.models import Customer, Price, Subscription

from apps.teams.models import Team
from apps.utils.billing import get_stripe_module

from .helpers import provision_subscription

logger = logging.getLogger(__name__)


@djstripe_receiver("checkout.session.completed")
def checkout_session_completed(event, **kwargs):
    """
    This webhook is called when a customer signs up for a subscription via Stripe Checkout.

    We must then provision the subscription and assign it to the appropriate user/team.
    """
    session = event.data["object"]
    # only process subscriptions created by this module or that have a subscription set
    if session["metadata"].get("source") == "subscriptions" or session.get("subscription"):
        client_reference_id = session.get("client_reference_id")
        subscription_id = session.get("subscription")
        subscription_holder = Team.objects.get(id=client_reference_id)
        provision_subscription(subscription_holder, subscription_id)


@djstripe_receiver("customer.subscription.updated")
def update_customer_subscription(event, **kwargs):
    """
    This webhook is called when a customer updates their subscription via the Stripe
    billing portal.

    There are a few scenarios this can happen - if they are upgrading, downgrading
    cancelling (at the period end) or renewing after a cancellation.

    We update the subscription in place based on the possible fields, and
    these updates automatically trickle down to the user/team that holds the subscription.

    Stripe docs: https://stripe.com/docs/customer-management/integrate-customer-portal#webhooks
    """
    # check if we can handle this change
    if has_multiple_items(event.data):
        logging.warning("Not processing changes to Stripe subscription because it has multiple products.")
        return

    new_price = get_price_data(event.data)
    subscription_id = get_subscription_id(event.data)

    # find associated subscription and sync from Stripe to capture all changes
    # (cancel_at_period_end, price, etc. are stored in stripe_data in dj-stripe 2.10+)
    dj_subscription = Subscription.objects.get(id=subscription_id)
    subscription_item = dj_subscription.items.get()
    subscription_item.price = Price.objects.get(id=new_price["id"])
    subscription_item.save()
    # sync the full subscription from Stripe to update stripe_data
    # (fields like cancel_at_period_end are no longer DB columns)
    # Note: sync_from_stripe_data expects a Stripe API object, not a raw webhook dict,
    # so we retrieve it from the API first.
    stripe = get_stripe_module()
    stripe_sub = stripe.Subscription.retrieve(subscription_id)
    Subscription.sync_from_stripe_data(stripe_sub)


@djstripe_receiver("customer.subscription.deleted")
def email_admins_when_subscriptions_canceled(event, **kwargs):
    # example webhook handler to notify admins when a subscription is deleted/canceled
    try:
        customer_email = Customer.objects.get(id=event.data["object"]["customer"]).email
    except Customer.DoesNotExist:
        customer_email = "unavailable"

    mail_admins(
        "Someone just canceled their subscription!",
        f"Their email was {customer_email}",
        fail_silently=True,
    )


def has_multiple_items(stripe_event_data):
    return len(stripe_event_data["object"]["items"]["data"]) > 1


def get_price_data(stripe_event_data):
    return stripe_event_data["object"]["items"]["data"][0]["price"]


def get_subscription_id(stripe_event_data):
    return stripe_event_data["object"]["items"]["data"][0]["subscription"]


def get_cancel_at_period_end(stripe_event_data):
    return stripe_event_data["object"]["cancel_at_period_end"]
