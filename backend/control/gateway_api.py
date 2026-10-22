"""Dedicated internal WSGI app. Never mount this on the public panel listener."""

import hmac
import json
import logging
import re
import stat

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections

from .connections import track
from .gateway_auth import TooManyConnections, exchange


def load_key():
    path = settings.SHINYD_GATEWAY_KEY_FILE
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027:
        raise ValueError("Chave do gateway exige arquivo regular privado (0600 ou 0640).")
    key = path.read_text().strip()
    if not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("Chave do gateway inválida.")
    return key


class GatewayAPI:
    def __init__(self, key):
        self.key = key

    def __call__(self, environ, start_response):
        close_old_connections()
        try:
            status, body = self.dispatch(environ)
        except PermissionDenied:
            status, body = "403 Forbidden", {"allowed": False}
        except TooManyConnections:
            status, body = "429 Too Many Requests", {"allowed": False}
        except (ValueError, TypeError, KeyError, UnicodeError):
            status, body = "400 Bad Request", {"allowed": False}
        except Exception:
            # Deliberately omit body, traceback and headers: they contain credentials.
            logging.getLogger(__name__).error("Autorizador indisponível; acesso negado.")
            status, body = "503 Service Unavailable", {"allowed": False}
        finally:
            close_old_connections()
        data = json.dumps(body).encode()
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
                ("Content-Length", str(len(data))),
            ],
        )
        return [data]

    def dispatch(self, env):
        supplied = env.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + self.key).encode()):
            raise PermissionDenied
        if env.get("REQUEST_METHOD") != "POST":
            return "405 Method Not Allowed", {"allowed": False}
        operation = env.get("PATH_INFO")
        # Legacy authorization cannot safely serve untracked requests during drain.
        if operation not in {"/v2/request", "/v1/exchange"} or env.get("QUERY_STRING"):
            return "404 Not Found", {"allowed": False}
        length = int(env.get("CONTENT_LENGTH") or 0)
        if length < 1 or length > 2048 or env.get("CONTENT_TYPE") != "application/json":
            return "400 Bad Request", {"allowed": False}
        data = json.loads(env["wsgi.input"].read(length))
        expected = (
            {"operation", "request_id", "host", "credential", "kind"}
            if operation == "/v2/request"
            else {"host", "code"}
        )
        if not isinstance(data, dict) or set(data) != expected:
            return "400 Bad Request", {"allowed": False}
        if any(not isinstance(value, str) for value in data.values()) or len(data["host"]) > 259:
            return "400 Bad Request", {"allowed": False}
        result = track(**data) if operation == "/v2/request" else exchange(**data)
        return "200 OK", result
