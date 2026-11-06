from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Create the database cache table used by the face index
    call_command('createcachetable', database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
