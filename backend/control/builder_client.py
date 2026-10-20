"""No subprocess/sudo in the panel. Files cross a mutually authenticated Unix socket."""

import http.client
import os
import stat

from django.conf import settings

from builder.policy import MESSAGES, canonical, strict_json
from builder.protocol import validate_result

from .agent_client import AgentUnavailable, UnixConnection


class BuilderUnavailable(Exception):
    pass


class BuilderRejected(Exception):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "storage"
        super().__init__(MESSAGES[self.code])


def request(method, path, *, body=None, headers=None, timeout=3):
    connection = UnixConnection(settings.SHINYD_BUILDER_SOCKET, 0)
    connection.host = "builder"
    connection.timeout = timeout
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={"Connection": "close", **(headers or {})},
        )
        response = connection.getresponse()
        raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError
        value = strict_json(raw)
        if response.status == 404:
            return None
        if response.status != 200:
            if response.status in {400, 409, 429}:
                raise BuilderRejected(value.get("error"))
            raise BuilderUnavailable
        return value
    except (OSError, http.client.HTTPException, AgentUnavailable, ValueError, TypeError) as exc:
        raise BuilderUnavailable("Preparador indisponível. A consulta será retomada.") from exc
    finally:
        connection.close()


def environments():
    value = request("GET", "/v1/environments")
    try:
        entries = value["environments"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
            raise ValueError
        from builder.policy import R_VERSION, SLUG

        if any(
            set(item) != {"id", "r_version"}
            or not SLUG.fullmatch(item["id"])
            or not R_VERSION.fullmatch(item["r_version"])
            for item in entries
        ):
            raise ValueError
        return entries
    except (KeyError, TypeError, ValueError) as exc:
        raise BuilderUnavailable from exc


def spec_for(job):
    return {
        "app_id": str(job.app_id),
        "source_hash": job.version.source_hash,
        "entrypoint": job.version.entrypoint,
        "environment": job.build_environment,
    }


def checked(value, job):
    if value is None:
        return None
    try:
        return validate_result(value, str(job.pk), spec_for(job))
    except (ValueError, TypeError, KeyError) as exc:
        raise BuilderUnavailable from exc


def inspect(job):
    return checked(request("GET", f"/v1/builds/{job.pk}"), job)


def cancel(job):
    return checked(request("POST", f"/v1/builds/{job.pk}/cancel"), job)


def submit(job):
    # Never let a database path turn into a read of another file by the worker.
    import re

    if not re.fullmatch(r"sources/[a-f0-9-]{36}\.zip", job.version.source_path):
        raise BuilderRejected("archive")
    source = settings.STATE_DIR / job.version.source_path
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= settings.SHINYD_UPLOAD_LIMIT:
            raise BuilderRejected("archive")
        return checked(
            request(
                "PUT",
                f"/v1/builds/{job.pk}",
                body=stream,
                timeout=65,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(info.st_size),
                    "X-Shinyd-Build": canonical(spec_for(job)).decode(),
                },
            ),
            job,
        )
