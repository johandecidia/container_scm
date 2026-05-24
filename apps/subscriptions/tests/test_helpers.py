from django.test import SimpleTestCase

from apps.subscriptions.exceptions import InvalidPriceError
from apps.subscriptions.helpers import validate_stripe_price_id


class TestValidateStripePriceId(SimpleTestCase):
    def test_valid_price_ids(self):
        valid_ids = [
            "price_abc123",
            "price_ABC123def",
            "price_1MoBy5LkdIwHu7ixZhnattbh",
            "plan_abc123",
            "plan_ABC123def",
        ]
        for price_id in valid_ids:
            with self.subTest(price_id=price_id):
                validate_stripe_price_id(price_id)  # should not raise

    def test_invalid_price_ids(self):
        invalid_ids = [
            "",
            "not_a_price",
            "price_",
            "plan_",
            "prod_abc123",
            "price_abc 123",
            "price_abc/123",
            "1\udcf0\udcf0\udcf0\udcf0%2527%2522\\\\'\\\\\"",
            "<script>alert(1)</script>",
            "price_abc\x00def",
        ]
        for price_id in invalid_ids:
            with self.subTest(price_id=price_id), self.assertRaises(InvalidPriceError):
                validate_stripe_price_id(price_id)
