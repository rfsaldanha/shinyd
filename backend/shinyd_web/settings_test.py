import os

os.environ["SHINYD_SECRET_KEY"] = "test-only-" * 8
os.environ["SHINYD_DEV"] = "1"

from .settings import *

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
