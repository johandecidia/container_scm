# Stage one of three: make room.
#
# Conditions stop being a `TextChoices` enum on `Container.condition` and become
# per-team rows. Doing that in one step would mean altering a CharField into a
# ForeignKey with data in it, so it is staged instead:
#
#   0009  create the table, and a nullable `condition_ref` beside the old column
#   0010  read the strings that are actually there and point `condition_ref` at rows
#   0011  drop the old column and rename `condition_ref` to `condition`
#
# Nothing is destroyed until 0011, so 0009 and 0010 can run against a live database
# and be inspected before the column goes.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scm_containers", "0008_containermovement_affects_current_state_and_more"),
        ("teams", "0003_team_billing_details_last_changed_team_customer_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ContainerCondition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "code",
                    models.CharField(
                        help_text="Stable internal identity, e.g. CW. Changing it is not the same as renaming.",
                        max_length=30,
                        verbose_name="code",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="What operators see. Safe to change.", max_length=100, verbose_name="name"
                    ),
                ),
                ("sort_order", models.PositiveIntegerField(default=0, verbose_name="sort order")),
                ("is_active", models.BooleanField(default=True, verbose_name="active")),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="teams.team", verbose_name="Team"
                    ),
                ),
            ],
            options={
                "verbose_name": "Container Condition",
                "verbose_name_plural": "Container Conditions",
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.AddConstraint(
            model_name="containercondition",
            constraint=models.UniqueConstraint(
                fields=("team", "code"), name="unique_container_condition_code_per_team"
            ),
        ),
        migrations.AddField(
            model_name="container",
            name="condition_ref",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="containers",
                to="scm_containers.containercondition",
                verbose_name="condition",
            ),
        ),
    ]
