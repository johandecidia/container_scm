"""Drop the team-level stop-on-receive; ``scm_containers.0013`` copied it onto every location first."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("scm_tracking", "0017_teamtrackingsettings_stop_tracking_on_receive"),
        ("scm_containers", "0013_location_auto_stop_tracking_on_receive"),
    ]

    operations = [
        migrations.RemoveField(model_name="teamtrackingsettings", name="stop_tracking_on_receive"),
    ]
