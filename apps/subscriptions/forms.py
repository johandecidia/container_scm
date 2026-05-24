from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _
from djstripe.models import SubscriptionItem

from apps.subscriptions.models import SubscriptionModelBase
from apps.utils.billing import get_stripe_module


class UsageRecordForm(forms.Form):
    """
    Form for recording usage of a metered subscription.
    """

    subscription_item = forms.ModelChoiceField(SubscriptionItem.objects.none(), label=_("Product"))
    quantity = forms.IntegerField(initial=1, min_value=1)

    def __init__(self, subscription_holder: SubscriptionModelBase, *args, **kwargs):
        super().__init__(*args, **kwargs)
        subscription = subscription_holder.active_stripe_subscription
        if subscription is None:
            raise ValueError("UsageRecordForm requires an active subscription")
        # limit the subscription item choices to those matching the current subscription
        self.metered_plans = subscription.items.filter(
            price__stripe_data__recurring__usage_type="metered",
        )
        subscription_item_field = cast(forms.ModelChoiceField, self.fields["subscription_item"])
        subscription_item_field.queryset = self.metered_plans
        # display products in the dropdown
        subscription_item_field.label_from_instance = lambda item: item.price.product.name  # type: ignore[method-assign, assignment]

    def is_usable(self):
        return self.metered_plans.exists()

    def save(self):
        subscription_item = self.cleaned_data["subscription_item"]
        quantity = self.cleaned_data["quantity"]
        # UsageRecord model was removed in dj-stripe 2.10, use Stripe API directly
        stripe = get_stripe_module()
        return stripe.SubscriptionItem.create_usage_record(subscription_item.id, quantity=quantity)
