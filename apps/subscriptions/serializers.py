from datetime import UTC, datetime

from djstripe.models import Price, Product, Subscription, SubscriptionItem
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class FlexibleDateTimeField(serializers.DateTimeField):
    """DateTimeField that also handles unix timestamps (integers).

    dj-stripe 2.10+ returns integer timestamps from stripe_data property accessors
    instead of datetime objects.
    """

    def to_representation(self, value):
        if isinstance(value, (int, float)):
            value = datetime.fromtimestamp(value, tz=UTC)
        return super().to_representation(value)


class PriceSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name")
    human_readable_price = serializers.SerializerMethodField()
    payment_amount = serializers.SerializerMethodField()
    # unit_amount is a property accessor on stripe_data in dj-stripe 2.10+
    unit_amount = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.STR)
    def get_human_readable_price(self, obj):
        # this needs to be here because djstripe can return a proxy object which can't be serialized
        return str(obj.human_readable_price)

    @extend_schema_field(OpenApiTypes.STR)
    def get_payment_amount(self, obj):
        if self.context.get("product_metadata", None):
            return self.context["product_metadata"].get_price_display(obj)
        return str(obj.human_readable_price)

    @extend_schema_field(OpenApiTypes.INT)
    def get_unit_amount(self, obj):
        return obj.unit_amount

    class Meta:
        model = Price
        fields = ("id", "product_name", "human_readable_price", "payment_amount", "nickname", "unit_amount")


class SubscriptionItemSerializer(serializers.ModelSerializer):
    price = PriceSerializer()
    # quantity is a property accessor on stripe_data in dj-stripe 2.10+
    quantity = serializers.IntegerField(read_only=True)

    class Meta:
        model = SubscriptionItem
        fields = ("id", "price", "quantity")


class SubscriptionSerializer(serializers.ModelSerializer):
    """
    A serializer for Subscriptions which uses the SubscriptionWrapper object under the hood
    """

    items = SubscriptionItemSerializer(many=True)
    display_name = serializers.CharField()
    billing_interval = serializers.CharField()
    # These fields are property accessors on stripe_data in dj-stripe 2.10+
    start_date = FlexibleDateTimeField(read_only=True)
    current_period_start = FlexibleDateTimeField(read_only=True)
    current_period_end = FlexibleDateTimeField(read_only=True)
    cancel_at_period_end = serializers.BooleanField(read_only=True)
    status = serializers.CharField(read_only=True)
    quantity = serializers.IntegerField(read_only=True)

    class Meta:
        # we use Subscription instead of SubscriptionWrapper so DRF can infer the model-based fields automatically
        model = Subscription
        fields = (
            "id",
            "display_name",
            "start_date",
            "billing_interval",
            "current_period_start",
            "current_period_end",
            "cancel_at_period_end",
            "status",
            "quantity",
            "items",
        )


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name")
