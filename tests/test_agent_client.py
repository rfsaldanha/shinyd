import json
import os
import socketserver
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler

import pytest
from django.urls import reverse

from control.agent_client import AgentUnavailable, read_status


def report():
    return {
        "schema_version": 1,
        "mode": "diagnostic",
        "capabilities": ["status"],
        "runtime_enabled": False,
        "report": {
            "schema_version": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "os": "linux",
            "architecture": "amd64",
            "prerequisites_ready": True,
            "notice": "Diagnóstico somente leitura.",
            "checks": [
                {
                    "code": "cgroup.cpu",
                    "status": "pass",
                    "required": True,
                    "detail": "Controlador CPU observado",
                }
            ],
        },
    }


@pytest.fixture
def agent_socket(settings, tmp_path):
    servers = []
    settings.DEBUG = True
    settings.SHINYD_AGENT_DEV_UID = os.getuid()

    def start(data=None, code=200, raw=None):
        path = str(tmp_path / f"agent{len(servers)}.sock")
        body = (
            raw if raw is not None else json.dumps(data if data is not None else report()).encode()
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = socketserver.UnixStreamServer(path, Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        settings.SHINYD_AGENT_SOCKET = path
        return path

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_agent_reads_kernel_authenticated_socket(agent_socket):
    agent_socket()
    result = read_status()
    assert result["report"]["checks"][0]["code"] == "cgroup.cpu"
    assert result["runtime_enabled"] is False


def test_agent_rejects_impersonating_peer(agent_socket, settings):
    agent_socket()
    settings.SHINYD_AGENT_DEV_UID = os.getuid() + 1
    with pytest.raises(AgentUnavailable, match="identidade"):
        read_status()


@pytest.mark.skipif(os.getuid() == 0, reason="Checks a non-root impersonator")
def test_production_ignores_development_uid(agent_socket, settings):
    agent_socket()
    settings.DEBUG = False
    with pytest.raises(AgentUnavailable, match="identidade"):
        read_status()


@pytest.mark.parametrize("code", [302, 403, 404, 500])
def test_agent_error_is_safe_and_does_not_follow_redirects(agent_socket, code):
    agent_socket(code=code, raw=b"secret-upstream-body")
    with pytest.raises(AgentUnavailable) as error:
        read_status()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["old", "future", "schema", "runtime", "checks", "ready"])
def test_agent_rejects_stale_or_incompatible_report(agent_socket, kind):
    data = report()
    if kind in ("old", "future"):
        seconds = -120 if kind == "old" else 60
        data["report"]["observed_at"] = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    elif kind == "schema":
        data["schema_version"] = 2
    elif kind == "runtime":
        data["runtime_enabled"] = True
    elif kind == "checks":
        data["report"]["checks"] = [None]
    else:
        data["report"]["prerequisites_ready"] = False
    agent_socket(data=data)
    with pytest.raises(AgentUnavailable, match="inválido ou desatualizado"):
        read_status()


@pytest.mark.parametrize("raw", [b"{", b"x" * 65537])
def test_agent_rejects_malformed_and_oversized_reply(agent_socket, raw):
    agent_socket(raw=raw)
    with pytest.raises(AgentUnavailable):
        read_status()


@pytest.mark.parametrize("path", ["https://agent/", "relative.sock", "/run/../tmp/agent"])
def test_agent_never_accepts_url_or_relative_path(settings, path):
    settings.SHINYD_AGENT_SOCKET = path
    with pytest.raises(AgentUnavailable, match="configuração"):
        read_status()


@pytest.mark.django_db
def test_diagnostics_only_admin_without_contacting_agent(client, accounts, monkeypatch):
    def unexpected():
        pytest.fail("An unauthorized account contacted the agent")

    monkeypatch.setattr("control.agent_views.read_status", unexpected)
    url = reverse("diagnostics")
    assert client.get(url).status_code == 302
    for role in ("publisher", "viewer", "outsider"):
        client.force_login(accounts[role])
        assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_diagnostics_escape_details_and_explain_limits(client, accounts, agent_socket):
    data = report()
    data["report"]["checks"][0]["detail"] = "<script>alert('unsafe')</script>"
    agent_socket(data=data)
    client.force_login(accounts["admin"])
    response = client.get(reverse("diagnostics"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Agente conectado" in content and "Consultas somente de leitura" in content
    assert "&lt;script&gt;" in content and "<script>alert" not in content
    assert response["Cache-Control"] == "no-store"
    assert client.post(reverse("diagnostics")).status_code == 405


@pytest.mark.django_db
def test_agent_outage_does_not_break_dashboard(client, accounts, settings, tmp_path):
    settings.SHINYD_AGENT_SOCKET = str(tmp_path / "missing.sock")
    client.force_login(accounts["admin"])
    response = client.get(reverse("diagnostics"))
    assert response.status_code == 200
    assert "Diagnóstico indisponível" in response.content.decode()
    assert str(tmp_path) not in response.content.decode()
    assert client.get("/").status_code == 200
