"""The container conditions a team starts with, and how it gets them.

One list and one function. Conditions are a team's own master data — see
:class:`~apps.scm.containers.models.ContainerCondition` — so this is a *starting
point*, not a schema: a team is expected to rename, reorder and retire these, and
nothing in the codebase reads the codes below to decide anything.

The set is the industry grading vocabulary MCR sells against, in the order it is
usually quoted in:

.. code-block:: text

    NEW   New
    IICL  IICL
    CW    Cargo Worthy
    WW    Wind & Water Tight
    AI    As is

The names are plain strings rather than translated ones on purpose. They become rows
in a table an operator edits, and a lazily-translated label would store whatever
language the person who created the team happened to be using — then stay in that
language for everybody else. UI text is translated; master data is typed.
"""

from apps.teams.models import Team

from .models import ContainerCondition

# (code, name), in display order. `sort_order` is derived from the position in tens,
# so a team can slot something between two of these without renumbering the rest.
DEFAULT_CONTAINER_CONDITIONS: tuple[tuple[str, str], ...] = (
    ("NEW", "New"),
    ("IICL", "IICL"),
    ("CW", "Cargo Worthy"),
    ("WW", "Wind & Water Tight"),
    ("AI", "As is"),
)


def ensure_default_conditions(team: Team) -> list[ContainerCondition]:
    """Give *team* the default conditions it does not already have.

    Idempotent, and matched on ``code`` — the stable identity — so a team that has
    renamed "As is" to something clearer keeps its wording rather than having it
    reset. Nothing is deactivated or removed here either: a condition the team chose
    to retire stays retired.
    """
    conditions = []
    for position, (code, name) in enumerate(DEFAULT_CONTAINER_CONDITIONS):
        condition, _created = ContainerCondition.objects.get_or_create(
            team=team,
            code=code,
            defaults={"name": name, "sort_order": position * 10},
        )
        conditions.append(condition)
    return conditions
