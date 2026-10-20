"""Read-only client for the executor's private Unix socket."""

import http.client
import json
import socket
import struct
from datetime import UTC, datetime
from pathlib import PurePosixPath

from django.conf import settings


class AgentUnavailable(Exception):
    """A safe message, without socket paths, response bodies or credentials."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, expected_uid):
        super().__init__("agent", timeout=3)
        self.path = path
        self.expected_uid = expected_uid

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != self.expected_uid:
                raise AgentUnavailable("A identidade do agente não pôde ser confirmada.")
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def validate_status(data):
    """Reject incompatible, incomplete or stale responses instead of showing green."""
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("mode") != "diagnostic"
        or data.get("capabilities") != ["status"]
        or data.get("runtime_enabled") is not False
    ):
        raise ValueError("invalid status")
    report = data.get("report")
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or type(report.get("prerequisites_ready")) is not bool
        or not isinstance(report.get("checks"), list)
        or not 1 <= len(report["checks"]) <= 64
    ):
        raise ValueError("invalid report")
    for name in ("os", "architecture", "notice", "observed_at"):
        if not isinstance(report.get(name), str) or len(report[name]) > 4096:
            raise ValueError("invalid text")
    observed = datetime.fromisoformat(report["observed_at"])
    if observed.tzinfo is None or not -5 <= (datetime.now(UTC) - observed).total_seconds() <= 60:
        raise ValueError("stale report")
    for check in report["checks"]:
        if (
            not isinstance(check, dict)
            or check.get("status") not in ("pass", "fail", "unknown", "warning")
            or type(check.get("required")) is not bool
            or not isinstance(check.get("code"), str)
            or not 1 <= len(check["code"]) <= 100
            or not isinstance(check.get("detail"), str)
            or len(check["detail"]) > 4096
        ):
            raise ValueError("invalid check")
    ready = all(c["status"] == "pass" for c in report["checks"] if c["required"])
    if ready != report["prerequisites_ready"]:
        raise ValueError("inconsistent report")
    report["observed_at"] = observed
    return data


def read_status():
    path = str(settings.SHINYD_AGENT_SOCKET)
    # Configuration is local; a URL or user-provided path is never accepted.
    if not path.startswith("/") or str(PurePosixPath(path)) != path or ".." in path.split("/"):
        raise AgentUnavailable("A configuração do agente precisa ser revisada.")
    # A development override cannot weaken server authentication in production.
    expected_uid = settings.SHINYD_AGENT_DEV_UID if settings.DEBUG else 0
    connection = UnixConnection(path, expected_uid)
    try:
        connection.request("GET", "/v1/status", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status == 403:
            raise AgentUnavailable("O agente recusou a identidade do serviço de controle.")
        if response.status != 200:
            raise AgentUnavailable("O agente não pôde atender à consulta.")
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("oversized response")
        return validate_status(json.loads(payload))
    except (OSError, http.client.HTTPException) as exc:
        raise AgentUnavailable(
            "Agente indisponível. Verifique a instalação e o estado do serviço."
        ) from exc
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise AgentUnavailable(
            "O agente retornou um diagnóstico inválido ou desatualizado."
        ) from exc
    finally:
        connection.close()
