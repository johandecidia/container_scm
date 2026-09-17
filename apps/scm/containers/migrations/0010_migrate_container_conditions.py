# Stage two of three: move the data. See 0009 for the staging.
#
# The conditions this creates come from *what is in the database*, not from what the
# enum used to say. The choices have been edited since the initial migration, so a
# list copied out of the code would invent rows for values no container carries and
# miss the ones they do. `values_list("team_id", "condition").distinct()` is the only
# honest source.
#
# Two kinds of row come out of it:
#
# *The default vocabulary*, seeded for every team — the same five
# `apps.scm.containers.conditions.DEFAULT_CONTAINER_CONDITIONS` gives a new team, so
# an existing team is not left with a shorter list than one created a minute later.
# Seeded for teams with no containers too, since a team with an empty condition list
# could not grade its first box.
#
# *Whatever else is actually in use*, created **inactive**. `GOOD`, `FAIR` and
# `DAMAGED` were the original vocabulary and were deliberately replaced; the
# containers graded with them must keep saying so, and nobody should be offered them
# again. Inactive is exactly that distinction, and the names are the labels those
# codes carried in 0001 — the value stored on the row, not a new invention.
from django.db import migrations

# (code, name) in display order, kept as a literal rather than imported: a migration
# has to describe the past, and `conditions.py` is free to change.
DEFAULTS = (
    ("NEW", "New"),
    ("IICL", "IICL"),
    ("CW", "Cargo Worthy"),
    ("WW", "Wind & Water Tight"),
    ("AI", "As is"),
)

# The labels the retired codes carried in 0001. A code not listed here — one written
# by hand or by an importer — keeps the code itself as its name, which is the only
# thing known about it.
RETIRED_NAMES = {
    "GOOD": "Good",
    "FAIR": "Fair",
    "DAMAGED": "Damaged",
}

DEFAULT_CODES = {code for code, _name in DEFAULTS}


def create_conditions_and_link_containers(apps, schema_editor):
    Team = apps.get_model("teams", "Team")
    Container = apps.get_model("scm_containers", "Container")
    ContainerCondition = apps.get_model("scm_containers", "ContainerCondition")

    # code -> row, per team, built once and reused for both passes.
    by_team: dict[int, dict[str, object]] = {}

    def condition_for(team_id, code, *, name, sort_order, is_active):
        known = by_team.setdefault(team_id, {})
        if code in known:
            return known[code]
        condition, _created = ContainerCondition.objects.get_or_create(
            team_id=team_id,
            code=code,
            defaults={"name": name, "sort_order": sort_order, "is_active": is_active},
        )
        known[code] = condition
        return condition

    for team_id in Team.objects.values_list("id", flat=True):
        for position, (code, name) in enumerate(DEFAULTS):
            condition_for(team_id, code, name=name, sort_order=position * 10, is_active=True)

    in_use = Container.objects.exclude(condition="").values_list("team_id", "condition").distinct()
    for team_id, code in in_use:
        if code in DEFAULT_CODES:
            continue
        condition_for(
            team_id,
            code,
            name=RETIRED_NAMES.get(code, code),
            # After the defaults, in the order they are found. Retired rows are not
            # offered, so their position only matters in the Settings list.
            sort_order=len(DEFAULTS) * 10 + len(by_team[team_id]),
            is_active=False,
        )

    for team_id, codes in by_team.items():
        for code, condition in codes.items():
            Container.objects.filter(team_id=team_id, condition=code).update(condition_ref=condition.pk)


def unlink_containers(apps, schema_editor):
    """Undo the linking. The rows are left behind — deleting master data is not a rollback."""
    Container = apps.get_model("scm_containers", "Container")
    Container.objects.update(condition_ref=None)


class Migration(migrations.Migration):
    dependencies = [
        ("scm_containers", "0009_containercondition"),
    ]

    operations = [
        migrations.RunPython(create_conditions_and_link_containers, unlink_containers),
    ]
