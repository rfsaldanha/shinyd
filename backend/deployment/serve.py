"""Fixed, unprivileged entry points; no shell or user-provided command strings."""

import os
import sys

from deployment.config import environment, load, origin


def main():
    mode, *arguments = sys.argv[1:]
    if os.geteuid() == 0:
        raise ValueError("Serviços de controle não devem executar como root.")
    config = load()
    listen = {
        k: os.environ[k] for k in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES") if k in os.environ
    }
    os.environ.clear()
    os.environ.update(environment(config))
    os.environ.update(listen)
    if mode == "prepare" and not arguments:
        from deployment.prepare import prepare

        prepare()
    elif mode == "gateway":
        os.execv(
            "/usr/libexec/shinyd/shinyd-gateway",
            [
                "shinyd-gateway",
                "--socket-activation",
                "--control-socket",
                "/run/shinyd-auth/http.sock",
                "--key-file",
                "/etc/shinyd-gateway/key",
                "--panel-origin",
                origin(config),
            ],
        )
    elif mode in {"panel", "auth"} and not arguments:
        from gunicorn.app.wsgiapp import WSGIApplication

        for key in listen:
            os.environ.pop(key, None)
        app = (
            "shinyd_web.wsgi:application"
            if mode == "panel"
            else "shinyd_web.gateway_wsgi:application"
        )
        # systemd owns socket permissions; umask remains private for DB/uploads.
        sys.argv = [
            "gunicorn",
            "--bind",
            "fd://3",
            "--workers",
            "1",
            "--threads",
            "4",
            "--umask",
            "0077",
            "--timeout",
            "120",
            "--worker-tmp-dir",
            "/tmp",
            app,
        ]
        WSGIApplication().run()
    elif mode in {"worker", "maintenance", "manage"}:
        from django.core.management import execute_from_command_line

        if mode == "worker":
            arguments = ["worker"]
        elif mode == "maintenance":
            execute_from_command_line(["shinyd-admin", "cleargateway"])
            arguments = ["clearsessions"]
        execute_from_command_line(["shinyd-admin", *arguments])
    else:
        raise ValueError("Operação desconhecida.")


if __name__ == "__main__":
    main()
