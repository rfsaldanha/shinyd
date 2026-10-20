import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SHINYD_STATE_DIR", BASE_DIR / "var")).resolve()
DEBUG = os.environ.get("SHINYD_DEV") == "1"
SECRET_KEY = os.environ.get("SHINYD_SECRET_KEY", "")
if not SECRET_KEY:
    try:
        key_path = STATE_DIR / "keys" / "django.key"
        if key_path.stat().st_mode & 0o077:
            raise ImproperlyConfigured("A chave Django deve ter permissão 0600.")
        SECRET_KEY = key_path.read_text().strip()
    except FileNotFoundError as e:
        raise ImproperlyConfigured("Execute scripts/bootstrap.py antes de iniciar.") from e
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("Chave Django inválida.")

ALLOWED_HOSTS = os.environ.get("SHINYD_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [v for v in os.environ.get("SHINYD_CSRF_ORIGINS", "").split(",") if v]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "control.apps.ControlConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "control.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "shinyd_web.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "shinyd_web.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": STATE_DIR / "metadata.sqlite3",
        "OPTIONS": {"timeout": 10, "transaction_mode": "IMMEDIATE"},
    }
}
AUTH_USER_MODEL = "control.User"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
PASSWORD_RESET_TIMEOUT = 3600
SESSION_COOKIE_NAME = "shinyd_dev_session" if DEBUG else "__Host-shinyd_session"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 12 * 60 * 60
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_NAME = "shinyd_dev_csrf" if DEBUG else "__Host-shinyd_csrf"
SECURE_SSL_REDIRECT = not DEBUG
# Only an explicitly configured local proxy may supply this header in deployment.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
# Preloading an operator's domain is an explicit deployment decision.
SECURE_HSTS_PRELOAD = False
SILENCED_SYSTEM_CHECKS = ["security.W021"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "dist" / "static"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_HANDLERS = [
    "control.uploads.BoundedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
SHINYD_UPLOAD_LIMIT = 500 * 1024 * 1024
SHINYD_EXTRACT_LIMIT = 2 * 1024 * 1024 * 1024
SHINYD_ZIP_ENTRIES = 50_000
SHINYD_MASTER_KEY_FILE = STATE_DIR / "keys" / "secrets.key"
# No execution before the runtime's safety and session gates are homologated.
SHINYD_RUNTIME_ENABLED = os.environ.get("SHINYD_RUNTIME_ENABLED") == "1"
SHINYD_RUNTIME_SOCKET = os.environ.get("SHINYD_RUNTIME_SOCKET", "/run/shinyd-runtime/control.sock")
SHINYD_PANEL_ORIGIN = os.environ.get("SHINYD_PANEL_ORIGIN", "")
SHINYD_APP_DOMAIN = os.environ.get("SHINYD_APP_DOMAIN", "")
SHINYD_GATEWAY_KEY_FILE = Path(
    os.environ.get("SHINYD_GATEWAY_KEY_FILE", STATE_DIR / "keys" / "gateway.key")
)
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
SHINYD_AGENT_SOCKET = os.environ.get("SHINYD_AGENT_SOCKET", "/run/shinyd-agent/control.sock")
SHINYD_AGENT_DEV_UID = int(os.environ.get("SHINYD_AGENT_DEV_UID", "0")) if DEBUG else 0
SHINYD_BUILDER_SOCKET = os.environ.get("SHINYD_BUILDER_SOCKET", "/run/shinyd-builder/control.sock")
