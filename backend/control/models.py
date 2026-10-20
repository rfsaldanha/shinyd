import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser, Group
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.crypto import salted_hmac


class User(AbstractUser):
    can_publish = models.BooleanField("Pode criar aplicativos", default=False)
    session_revision = models.PositiveIntegerField(default=1)

    def _get_session_auth_hash(self, secret=None):
        return salted_hmac(
            "shinyd.User.session",
            f"{self.password}:{self.session_revision}",
            secret=secret,
            algorithm="sha256",
        ).hexdigest()


class PriorityGroup(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=80)
    weight = models.PositiveIntegerField()

    class Meta:
        ordering = ["-weight"]
        constraints = [
            models.CheckConstraint(
                condition=Q(weight__gte=1, weight__lte=10000), name="priority_weight_range"
            )
        ]

    def __str__(self):
        return self.name


class Application(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE = "private", "Restrito"
        PUBLIC = "public", "Público"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Nome", max_length=120)
    slug = models.SlugField(
        "Endereço",
        unique=True,
        max_length=40,
        validators=[
            RegexValidator(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", "Use letras minúsculas e hífens.")
        ],
    )
    description = models.TextField("Descrição", blank=True, max_length=4000)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    priority = models.ForeignKey(PriorityGroup, on_delete=models.PROTECT)
    visibility = models.CharField(
        "Acesso", choices=Visibility, default=Visibility.PRIVATE, max_length=10
    )
    desired_state = models.CharField(default="stopped", max_length=20)
    revision = models.PositiveIntegerField(default=1)
    config_revision = models.PositiveIntegerField(default=0)
    active_publication = models.ForeignKey(
        "Publication", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AppGrant(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Acesso"
        PUBLISHER = "publisher", "Publicação"

    app = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE)
    role = models.CharField(max_length=12, choices=Role)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, group__isnull=True)
                    | Q(user__isnull=True, group__isnull=False)
                ),
                name="grant_exactly_one_subject",
            ),
            models.UniqueConstraint(fields=["app", "user"], name="unique_app_user_grant"),
            models.UniqueConstraint(fields=["app", "group"], name="unique_app_group_grant"),
        ]


class Version(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="versions")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source_hash = models.CharField(max_length=64)
    source_path = models.CharField(max_length=200)
    entrypoint = models.CharField(max_length=300, blank=True)
    r_version = models.CharField(max_length=30, blank=True)
    image_digest = models.CharField(max_length=256, blank=True)
    manifest = models.JSONField(default=dict)
    status = models.CharField(default="received", max_length=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Publication(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app = models.ForeignKey(Application, on_delete=models.CASCADE)
    version = models.ForeignKey(Version, on_delete=models.PROTECT)
    config_revision = models.PositiveIntegerField()
    status = models.CharField(default="candidate", max_length=30)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class EnvironmentValue(models.Model):
    app = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="environment")
    name = models.CharField(
        max_length=128,
        validators=[RegexValidator(r"^[A-Z][A-Z0-9_]*$", "Use nome de variável em maiúsculas.")],
    )
    is_secret = models.BooleanField(default=True)
    # Encrypt non-secret environment values too; no accidental plaintext persistence.
    ciphertext = models.BinaryField()
    revision = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["app", "name"], name="unique_env_name")]


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Na fila"
        RUNNING = "running", "Em andamento"
        SUCCEEDED = "succeeded", "Concluído"
        FAILED = "failed", "Falhou"
        CANCELLED = "cancelled", "Cancelado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app = models.ForeignKey(Application, on_delete=models.CASCADE)
    version = models.ForeignKey(Version, null=True, on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True)
    status = models.CharField(max_length=12, choices=Status, default=Status.QUEUED)
    phase = models.CharField(max_length=50, default="queued")
    message = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    cancel_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=100)
    # Only allowlisted metadata is supplied by domain services.
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()


class AppInstance(models.Model):
    """Gateway registration; only the trusted executor may provision these records."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="instances")
    host = models.CharField(max_length=259, unique=True)
    target = models.CharField(max_length=100)
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class GatewayTicket(models.Model):
    token_hash = models.CharField(max_length=64, unique=True)
    instance = models.ForeignKey(AppInstance, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    panel_session = models.ForeignKey("sessions.Session", on_delete=models.CASCADE)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True)


class GatewaySession(models.Model):
    token_hash = models.CharField(max_length=64, unique=True)
    instance = models.ForeignKey(AppInstance, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    panel_session = models.ForeignKey("sessions.Session", on_delete=models.CASCADE)
    expires_at = models.DateTimeField(db_index=True)
