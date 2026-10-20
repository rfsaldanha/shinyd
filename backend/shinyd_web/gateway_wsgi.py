import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shinyd_web.settings")
django.setup()

from control.gateway_api import GatewayAPI, load_key  # noqa: E402

application = GatewayAPI(load_key())
