import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_recovery_upgrade_preserves_apps_and_defaults_to_disabled():
    before, after = ("control", "0011_lifecycle_operations"), ("control", "0012_app_recovery")
    executor = MigrationExecutor(connection)
    executor.migrate([before])
    try:
        models = executor.loader.project_state([before]).apps
        user = models.get_model("control", "User").objects.create(username="recovery-migration")
        priority, _ = models.get_model("control", "PriorityGroup").objects.get_or_create(
            slug="normal", defaults={"name": "Normal", "weight": 200}
        )
        app = models.get_model("control", "Application").objects.create(
            name="Existing", slug="existing", owner=user, priority=priority, desired_state="running"
        )
        executor = MigrationExecutor(connection)
        executor.migrate([after])
        models = executor.loader.project_state([after]).apps
        row = models.get_model("control", "RecoveryState").objects.get(app_id=app.pk)
        assert not row.enabled and row.attempts == 0 and row.authorized_by_id is None
        assert row.state == "unobserved" and row.job_id is None
        assert (
            models.get_model("control", "Application").objects.get(pk=app.pk).desired_state
            == "running"
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
