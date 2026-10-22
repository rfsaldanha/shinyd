import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_upgrade_remembers_only_the_active_version():
    before, after = ("control", "0010_connection_drain"), ("control", "0011_lifecycle_operations")
    executor = MigrationExecutor(connection)
    executor.migrate([before])
    try:
        models = executor.loader.project_state([before]).apps
        user = models.get_model("control", "User").objects.create(username="migration-owner")
        priority, _ = models.get_model("control", "PriorityGroup").objects.get_or_create(
            slug="normal", defaults={"name": "Normal", "weight": 200}
        )
        apps = models.get_model("control", "Application")
        active = apps.objects.create(name="Active", slug="active", owner=user, priority=priority)
        inactive = apps.objects.create(
            name="Inactive", slug="inactive", owner=user, priority=priority
        )
        versions = models.get_model("control", "Version")
        publications = models.get_model("control", "Publication")
        for app, status in [(active, "active"), (inactive, "retired")]:
            version = versions.objects.create(app=app, created_by=user, status="prepared")
            pub = publications.objects.create(
                app=app, version=version, created_by=user, config_revision=0, status=status
            )
            if app == active:
                app.active_publication = pub
                app.save()
        executor = MigrationExecutor(connection)
        executor.migrate([after])
        apps = executor.loader.project_state([after]).apps.get_model("control", "Application")
        assert (
            apps.objects.get(pk=active.pk).last_version_id == active.active_publication.version_id
        )
        assert apps.objects.get(pk=inactive.pk).last_version_id is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
