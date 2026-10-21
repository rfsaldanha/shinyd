from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control", "0007_publication_environment_snapshot")]
    operations = [
        migrations.AddField(
            model_name="application", name="data_enabled", field=models.BooleanField(default=False)
        ),
        migrations.AddField(
            model_name="publication", name="data_cutover", field=models.BooleanField(default=False)
        ),
        migrations.AddField(
            model_name="publication",
            name="data_preflight_passed",
            field=models.BooleanField(default=False),
        ),
    ]
