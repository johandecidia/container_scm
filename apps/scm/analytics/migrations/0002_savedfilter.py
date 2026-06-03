import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scm_analytics", "0001_initial"),
        ("teams", "0003_team_billing_details_last_changed_team_customer_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedFilter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=100, verbose_name="name")),
                (
                    "view_key",
                    models.CharField(
                        choices=[
                            ("purchase_orders", "Purchase Orders"),
                            ("supplier_deliveries", "Supplier Deliveries"),
                            ("shipments", "Shipments"),
                            ("containers", "Containers"),
                            ("tracking", "Tracking"),
                            ("analytics", "Analytics"),
                        ],
                        max_length=50,
                        verbose_name="view",
                    ),
                ),
                ("params", models.JSONField(blank=True, default=dict, verbose_name="filter parameters")),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="teams.team",
                        verbose_name="Team",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="scm_saved_filters",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "Saved Filter",
                "verbose_name_plural": "Saved Filters",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["team", "user", "view_key"], name="scm_analyti_team_id_sf_idx"),
                ],
            },
        ),
    ]
