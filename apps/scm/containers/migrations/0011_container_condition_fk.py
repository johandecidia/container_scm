# Stage three of three: drop the string, keep the name. See 0009 for the staging.
#
# `condition_ref` was only ever a landing place for 0010's data; the field the rest
# of the codebase talks to is `condition`, so the old column goes and the FK takes
# its name. Nothing reads `condition_ref` outside these three migrations.
#
# The index has to be rebuilt rather than renamed: it covered `(team_id, condition)`,
# and after the swap the second column is `condition_id`. It is dropped before the
# field it references and re-added, explicitly named, once the FK is in place.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scm_containers", "0010_migrate_container_conditions"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="container",
            name="scm_contain_team_id_250507_idx",
        ),
        migrations.RemoveField(
            model_name="container",
            name="condition",
        ),
        migrations.RenameField(
            model_name="container",
            old_name="condition_ref",
            new_name="condition",
        ),
        migrations.AddIndex(
            model_name="container",
            index=models.Index(fields=["team", "condition"], name="container_team_condition_idx"),
        ),
    ]
