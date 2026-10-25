"""Stop-on-receive moves from the team to the destination location.

Each existing location takes its team's previous ``stop_tracking_on_receive``, so no
receive behaves differently the day this runs. A team without a settings row had the
default (off). ``scm_tracking.0018`` then drops the team column: the location is the
only policy from here on. Only locations are written — no container, movement or
tracking row is touched.
"""

from django.db import migrations, models


def copy_team_policy_to_locations(apps, schema_editor):
    ContainerLocation = apps.get_model("scm_containers", "ContainerLocation")
    TeamTrackingSettings = apps.get_model("scm_tracking", "TeamTrackingSettings")
    teams_on = TeamTrackingSettings.objects.filter(stop_tracking_on_receive=True).values_list("team_id", flat=True)
    ContainerLocation.objects.filter(team_id__in=list(teams_on)).update(auto_stop_tracking_on_receive=True)


def copy_location_policy_to_teams(apps, schema_editor):
    """Reverse: a team stops on receive if any of its locations did (the closest single answer)."""
    ContainerLocation = apps.get_model("scm_containers", "ContainerLocation")
    TeamTrackingSettings = apps.get_model("scm_tracking", "TeamTrackingSettings")
    teams_on = set(
        ContainerLocation.objects.filter(auto_stop_tracking_on_receive=True).values_list("team_id", flat=True)
    )
    for team_id in teams_on:
        TeamTrackingSettings.objects.update_or_create(team_id=team_id, defaults={"stop_tracking_on_receive": True})


class Migration(migrations.Migration):
    dependencies = [
        ("scm_containers", "0012_container_tracking_provider_override"),
        ("scm_tracking", "0017_teamtrackingsettings_stop_tracking_on_receive"),
    ]

    operations = [
        migrations.AddField(
            model_name="containerlocation",
            name="auto_stop_tracking_on_receive",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Stop tracking a container once it is received at this location. The receive is recorded either way."
                ),
                verbose_name="stop tracking on receive",
            ),
        ),
        migrations.RunPython(copy_team_policy_to_locations, copy_location_policy_to_teams),
    ]
