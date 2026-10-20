"""Typed, mutually authenticated Unix API. No sudo, arbitrary targets or commands."""

import http.client

from django.conf import settings

from builder.policy import canonical, strict_json

from .agent_client import AgentUnavailable, UnixConnection

MESSAGES = {
    "artifact_rejected": "O executor recusou o artefato. Confira o builder e a política instalada.",
    "invalid_request": "O executor recusou o contrato de ativação.",
    "instance_conflict": "O identificador já pertence a outra operação ou foi encerrado.",
    "ownership_mismatch": "O recurso encontrado não pertence a esta publicação.",
    "admission_denied": "Aguardando recursos disponíveis no servidor.",
    "runtime_busy": "O executor está ocupado. A operação será retomada.",
    "runtime_unavailable": "Executor indisponível. A consulta será retomada.",
    "application_readiness_failed": "O aplicativo não respondeu à verificação de funcionamento.",
    "process_protection_failed": "Não foi possível comprovar o isolamento do aplicativo.",
    "apparmor_verification_failed": "A proteção AppArmor não pôde ser comprovada.",
    "user_mapping_verification_failed": "O isolamento de usuários não pôde ser comprovado.",
    "cgroup_verification_failed": "O grupo de recursos não pôde ser comprovado.",
    "network_namespace_verification_failed": "O isolamento de rede não pôde ser comprovado.",
}


class RuntimeUnavailable(Exception):
    pass


class RuntimeRejected(Exception):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "runtime_unavailable"
        super().__init__(MESSAGES[self.code])


def request(method, identity, spec, *, retire=False):
    connection = UnixConnection(settings.SHINYD_RUNTIME_SOCKET, 0)
    connection.host, connection.timeout = "runtime", 65
    path = f"/v1/instances/{identity}" + ("/retire" if retire else "")
    try:
        connection.request(
            method,
            path,
            body=canonical(spec) if method != "GET" else None,
            headers={"Connection": "close", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read(8193)
        if len(raw) > 8192:
            raise ValueError
        value = strict_json(raw)
        if response.status == 404 and method == "GET":
            return None
        if response.status != 200:
            if response.status in {400, 409, 429} and isinstance(value, dict):
                raise RuntimeRejected(value.get("error"))
            raise RuntimeUnavailable
        required = {
            "id",
            "app_id",
            "recipe",
            "artifact",
            "source_hash",
            "priority",
            "state",
            "desired",
        }
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or value.keys() - required - {"target", "error", "image"}
            or value["id"] != str(identity)
            or value["recipe"] != ""
            or any(value.get(k) != v for k, v in spec.items())
            or value["state"]
            not in {
                "ready",
                "starting",
                "stopped",
                "stopping",
                "failed",
                "unavailable",
                "removing",
                "removed",
            }
            or value["desired"] not in {"running", "stopped", "removed"}
            or value.get("error", "") not in {"", *MESSAGES}
            or (
                value["state"] == "ready"
                and (
                    value.get("target") != f"unix:/run/shinyd-runtime/apps/{identity}.sock"
                    or value["desired"] != "running"
                )
            )
            or (value["state"] != "ready" and value.get("target"))
        ):
            raise ValueError
        return value
    except (
        OSError,
        http.client.HTTPException,
        AgentUnavailable,
        ValueError,
        TypeError,
        RecursionError,
    ) as exc:
        raise RuntimeUnavailable from exc
    finally:
        connection.close()


def inspect(publication):
    return request("GET", publication.pk, publication.runtime_spec)


def ensure(publication):
    return request("PUT", publication.pk, publication.runtime_spec)


def retire(publication):
    value = request("POST", publication.pk, publication.runtime_spec, retire=True)
    if value["state"] != "removed" or value["desired"] != "removed":
        raise RuntimeUnavailable
    return value
