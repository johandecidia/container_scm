import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("teams", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Container",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("container_number", models.CharField(max_length=20, unique=True, verbose_name="container number")),
                ("carrier", models.CharField(blank=True, max_length=100, verbose_name="carrier")),
                ("status", models.CharField(blank=True, max_length=50, verbose_name="status")),
                ("etd", models.DateField(blank=True, null=True, verbose_name="estimated time of departure")),
                ("eta", models.DateField(blank=True, null=True, verbose_name="estimated time of arrival")),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="teams.team",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "abstract": False,
            },
        ),
    ]
