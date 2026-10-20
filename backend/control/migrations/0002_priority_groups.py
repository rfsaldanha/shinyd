from django.db import migrations


def seed(apps, schema_editor):
    group = apps.get_model("control", "PriorityGroup")
    for slug, name, weight in [
        ("high", "Alta prioridade", 400),
        ("normal", "Normal", 200),
        ("low", "Baixa prioridade", 100),
    ]:
        group.objects.get_or_create(slug=slug, defaults={"name": name, "weight": weight})


class Migration(migrations.Migration):
    dependencies = [("control", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
