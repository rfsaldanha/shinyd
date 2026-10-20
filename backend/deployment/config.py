"""Configuration shared by the installer and installed service launchers."""

import ipaddress
import json
import re
import stat
from pathlib import Path

CONFIG = Path("/etc/shinyd/control.json")
STATE = Path("/var/lib/shinyd-control")
LIB = Path("/usr/lib/shinyd-control")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def hostname(value):
    if not isinstance(value, str) or len(value) > 200 or "." not in value:
        raise ValueError("Use nomes DNS completos, em minúsculas.")
    if any(not LABEL.fullmatch(label) for label in value.split(".")):
        raise ValueError("Nome DNS inválido.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("Use nomes DNS distintos para painel e apps.")


def validate(value):
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "panel_host",
        "app_domain",
        "https_port",
        "bind",
    }:
        raise ValueError("Configuração desconhecida.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Versão de configuração desconhecida.")
    panel, apps = hostname(value["panel_host"]), hostname(value["app_domain"])
    if panel == apps or panel.endswith("." + apps) or apps.endswith("." + panel):
        raise ValueError("Painel e domínio dos apps devem ser separados.")
    if type(value["https_port"]) is not int or not 1 <= value["https_port"] <= 65535:
        raise ValueError("Porta HTTPS inválida.")
    ipaddress.IPv4Address(value["bind"])
    return value


def load(path=CONFIG):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o027:
        raise ValueError("A configuração deve ser regular, de root e privada (0640).")
    return validate(json.loads(path.read_text()))


def origin(value):
    suffix = "" if value["https_port"] == 443 else ":" + str(value["https_port"])
    return "https://" + value["panel_host"] + suffix


def environment(value):
    validate(value)
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "SHINYD_STATE_DIR": str(STATE),
        "SHINYD_ALLOWED_HOSTS": value["panel_host"],
        "SHINYD_PANEL_ORIGIN": origin(value),
        "SHINYD_APP_DOMAIN": value["app_domain"],
        "SHINYD_HTTPS_PORT": str(value["https_port"]),
        "SHINYD_RUNTIME_ENABLED": "1",
        "DJANGO_SETTINGS_MODULE": "shinyd_web.settings",
    }


def caddyfile(value):
    validate(value)
    port, bind = value["https_port"], value["bind"]
    tls = "tls /etc/shinyd-tls/cert.pem /etc/shinyd-tls/key.pem"
    return f"""{{
    admin off
    auto_https off
    log {{
        output discard
    }}
}}
https://{value["panel_host"]}:{port} {{
    bind {bind}
    {tls}
    handle /static/* {{
        root * /usr/lib/shinyd-control
        file_server
    }}
    handle {{
        reverse_proxy unix//run/shinyd-panel/http.sock {{
            header_up X-Forwarded-Proto https
            header_up -Forwarded
        }}
    }}
}}
https://*.{value["app_domain"]}:{port} {{
    bind {bind}
    {tls}
    reverse_proxy unix//run/shinyd-gateway/http.sock {{
        header_up X-Forwarded-Proto https
        header_up -Forwarded
    }}
}}
"""
