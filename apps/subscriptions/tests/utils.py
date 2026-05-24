import uuid

from django.contrib.messages.storage import default_storage
from django.contrib.sessions.backends.file import SessionStore
from django.test import RequestFactory
from django.utils import timezone
from djstripe import enums
from djstripe.models import Customer, Plan, Price, Product, Subscription, SubscriptionItem

from apps.subscriptions.metadata import ProductMetadata
from apps.teams.models import Team


def create_subscription_for_team(subscription_holder: Team, product: ProductMetadata, metered=False):
    """Create a dummy subscription for the given product."""
    stripe_product = create_stripe_product(product, metered=metered)

    customer = create_customer()
    subscription = create_subscription(customer, stripe_product)

    subscription_holder.subscription = subscription
    subscription_holder.customer = customer
    subscription_holder.save(update_fields=["subscription", "customer"])

    return subscription


def create_subscription(customer, product):
    now = timezone.now()
    # In dj-stripe 2.10+, many fields moved to stripe_data
    subscription = Subscription.objects.create(
        id=_make_stripe_id("subscription"),
        livemode=False,
        customer=customer,
        stripe_data={
            "collection_method": enums.InvoiceCollectionMethod.charge_automatically,
            "status": enums.SubscriptionStatus.active,
            "current_period_start": int(now.timestamp()) - 2592000,  # ~1 month ago
            "current_period_end": int(now.timestamp()) + 2592000,  # ~1 month from now
        },
    )
    plan = Plan.objects.create(
        id=_make_stripe_id("plan"),
        livemode=False,
        stripe_data={
            "currency": "usd",
            "active": True,
            "interval": enums.PlanInterval.month,
            "product": product.id,
        },
    )
    SubscriptionItem.objects.create(
        id=_make_stripe_id("sub_item"),
        price=product.default_price,
        subscription=subscription,
        livemode=False,
        plan=plan,
        stripe_data={
            "quantity": 1,
        },
    )
    return subscription


def create_customer():
    customer = Customer.objects.create(
        id=_make_stripe_id("customer"),
        livemode=False,
    )
    return customer


def create_stripe_product(product_meta: ProductMetadata, metered=False):
    """Create stripe products. This assumes that all products
    are 'monthly recurring' subscriptions which should be sufficient for tests.
    """
    price_id = _make_stripe_id("price")

    recurring = {
        "meter": _make_stripe_id("meter") if metered else None,
        "interval": "month",
        "usage_type": "metered" if metered else "licensed",
        "interval_count": 1,
        "aggregate_usage": None,
        "trial_period_days": None,
    }

    # In dj-stripe 2.10+, many fields moved to stripe_data
    product = Product.objects.create(
        id=product_meta.stripe_id,
        name=product_meta.name,
        livemode=False,
        active=True,
        stripe_data={
            "description": str(product_meta.description),
            "type": enums.ProductType.service,
            "default_price": price_id,
        },
    )

    price = Price.objects.create(
        id=price_id,
        currency="usd",
        livemode=False,
        product=product,
        active=True,
        stripe_data={
            "type": enums.PriceType.recurring,
            "unit_amount": 100,
            "unit_amount_decimal": "100",
            "recurring": recurring,
        },
    )
    # Update product stripe_data with the price reference
    product.stripe_data["default_price"] = price.id
    product.save(update_fields=["stripe_data"])
    return product


def _make_stripe_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def get_mock_request(team, user):
    """
    Simulates a request object which can be passed directly to a mock view, with a team and logged-in user.
    """
    request = RequestFactory().get("/mock_view")
    request.user = user  # django.contrib.auth.middleware.AuthenticationMiddleware
    request.team = team  # apps.teams.middleware.TeamsMiddleware
    request.session = SessionStore()  # django.contrib.sessions.middleware.SessionMiddleware
    request._messages = default_storage(request)  # django.contrib.messages.middleware.MessageMiddleware
    return request
