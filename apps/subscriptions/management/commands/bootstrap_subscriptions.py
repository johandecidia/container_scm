from django.core.management import call_command
from django.core.management.base import BaseCommand
from djstripe.enums import PriceType
from djstripe.models import Account, Product
from stripe import AuthenticationError

from apps.subscriptions.metadata import ProductMetadata
from apps.utils.billing import create_stripe_api_keys_if_necessary, get_stripe_module


class Command(BaseCommand):
    help = "Bootstraps your Stripe subscriptions"

    def handle(self, **options):
        self.stdout.write("Syncing products and prices from Stripe")
        try:
            if create_stripe_api_keys_if_necessary():
                self.stdout.write("Added Stripe secret key to the database...")
            # dj-stripe 2.10+ requires a synced Account (djstripe_owner_account FK).
            # Sync it first so that product/price sync can link to it.
            self._ensure_account_synced()
            # due to an issue in djstripe sometimes failing on unsynced data,
            # we need to sync prices once before syncing both products and prices
            call_command("djstripe_sync_models", "price")
            call_command("djstripe_sync_models", "product", "price")
        except AuthenticationError:
            self.stderr.write(
                "\n======== ERROR ==========\n"
                "Failed to authenticate with Stripe! Check your Stripe key settings.\n"
                "More info: https://docs.saaspegasus.com/subscriptions#getting-started"
            )
        else:
            self.stdout.write("Done! Creating default product configuration")
            self._create_default_product_config()

    def _ensure_account_synced(self):
        """Ensure the Stripe Account exists in the dj-stripe DB.

        dj-stripe 2.10+ links every object to djstripe_owner_account.
        Without it, all sync operations skip with 'Account matching query does not exist'.
        """
        if Account.objects.exists():
            return
        self.stdout.write("No Stripe Account in DB — syncing from Stripe API...")
        stripe = get_stripe_module()
        stripe_account = stripe.Account.retrieve()
        Account.sync_from_stripe_data(stripe_account)
        self.stdout.write(f"  Synced Account {stripe_account.id}")

    def _create_default_product_config(self):
        # make the first product the default
        default = True
        product_metas = []
        for product in Product.objects.filter(active=True):
            # default_price and type are no longer DB fields in dj-stripe 2.10+
            if not product.default_price or product.default_price.type != PriceType.recurring:
                continue
            product_meta = ProductMetadata.from_stripe_product(
                product,
                description=f"The {product.name} plan",
                is_default=default,
                features=_get_features(product),
            )
            default = False
            product_metas.append(product_meta)

        self.stdout.write(
            "Copy/paste the following code into your `apps/subscriptions/metadata.py` file "
            "and then remove any products that you don't want to offer as a subscription:\n\n"
        )
        newline = "\n"
        self.stdout.write(
            f"ACTIVE_PRODUCTS = [{newline}    {f',{newline}    '.join(str(meta) for meta in product_metas)},{newline}]"
        )


def _get_features(product: Product) -> list[str]:
    features = product.stripe_data.get("marketing_features")
    return (
        [f["name"] for f in features]
        if features
        else [
            f"{product.name} Feature 1",
            f"{product.name} Feature 2",
            f"{product.name} Feature 3",
        ]
    )
