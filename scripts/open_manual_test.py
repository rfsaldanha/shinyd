#!/usr/bin/env python3
"""Open the installed loopback panel in a separate Chrome profile with one TLS key pinned."""

import argparse
import hashlib
import json
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from manual_test_common import CONFIG, ORIGIN, PUBLIC, ROOT, public_manifest, read_file


def profile_settings(public=PUBLIC, *, owner=0):
    info = public.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError("unsafe_public_directory")
    value = json.loads(read_file(public / "setup.json", owners={owner}))
    pem = read_file(public / "cert.pem", owners={owner})
    cert = x509.load_pem_x509_certificate(pem)
    if (
        value.get("schema_version") != 1
        or value.get("mode") != "local-manual-test"
        or value.get("configured") is not True
        or value.get("origin") != ORIGIN
        or value.get("config") != CONFIG
        or value.get("certificate_sha256") != hashlib.sha256(pem).hexdigest()
        or not cert.not_valid_before_utc <= datetime.now(UTC) < cert.not_valid_after_utc
    ):
        raise ValueError("invalid_public_profile")
    expected = public_manifest({"source_report_sha256": "", "builder": {"environments": {}}}, pem)
    if value.get("spki") != expected["spki"]:
        raise ValueError("certificate_pin_differs")
    names = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    if not {CONFIG["panel_host"], "*." + CONFIG["app_domain"]} <= set(names):
        raise ValueError("certificate_names_differ")
    return value, pem


def health(pem):
    context = ssl.create_default_context(cadata=pem.decode("ascii"))
    with socket.create_connection((CONFIG["bind"], CONFIG["https_port"]), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=CONFIG["panel_host"]) as connection:
            connection.sendall(
                (
                    "GET /healthz HTTP/1.1\r\nHost: "
                    + CONFIG["panel_host"]
                    + ":"
                    + str(CONFIG["https_port"])
                    + "\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            received = b""
            while b"\r\n" not in received and len(received) < 4096:
                block = connection.recv(4096 - len(received))
                if not block:
                    break
                received += block
            if received.split(b"\r\n", 1)[0] not in {b"HTTP/1.1 200 OK", b"HTTP/1.0 200 OK"}:
                raise ValueError("panel_health_failed")


def browser_arguments(value, profile):
    return [
        "/usr/bin/google-chrome",
        "--user-data-dir=" + str(profile),
        "--no-first-run",
        "--no-default-browser-check",
        "--no-proxy-server",
        "--host-resolver-rules=MAP *.shinyd.test 127.0.0.1",
        "--ignore-certificate-errors-spki-list=" + value["spki"],
        ORIGIN,
    ]


def wait_health(pem, *, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            health(pem)
            return
        except (ConnectionError, TimeoutError):
            pass
        except ValueError as exc:
            if str(exc) != "panel_health_failed":
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("panel_startup_timeout")
        time.sleep(min(0.25, remaining))


def check_browser(value):
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="shinyd-manual-browser-") as directory:
        args = browser_arguments(value, Path(directory))
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                directory,
                executable_path=args[0],
                args=args[2:-1],
                headless=True,
            )
            try:
                page = context.new_page()
                page.goto(ORIGIN)
                page.get_by_label("Usuário:", exact=True).wait_for()
                page.get_by_label("Senha:", exact=True).wait_for()
                for width in (1280, 360):
                    page.set_viewport_size({"width": width, "height": 800})
                    if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
                        raise ValueError("login_overflows_viewport")
            finally:
                context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Confere HTTPS sem abrir o navegador")
    mode.add_argument(
        "--check-browser", action="store_true", help="Confere também o login no Chrome sem janela"
    )
    args = parser.parse_args()
    try:
        if os.geteuid() == 0:
            raise ValueError("browser_must_not_run_as_root")
        value, pem = profile_settings()
        wait_health(pem)
        if args.check or args.check_browser:
            if args.check_browser:
                check_browser(value)
            print(
                json.dumps(
                    {
                        "passed": True,
                        "origin": ORIGIN,
                        "tls_verified": True,
                        "browser_checked": args.check_browser,
                    }
                )
            )
            return
        profile = ROOT / "var/manual-browser"
        profile.mkdir(mode=0o700, exist_ok=True)
        info = profile.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_browser_profile")
        subprocess.Popen(
            browser_arguments(value, profile),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("Painel aberto em uma janela separada do Chrome: " + ORIGIN)
    except (OSError, ValueError, KeyError, TypeError, x509.ExtensionNotFound):
        raise SystemExit(
            "Não foi possível abrir o teste local. Confira a preparação, inicie shinyd.target "
            "e use sua conta comum nesta máquina; consulte docs/TESTE_MANUAL.md."
        ) from None


if __name__ == "__main__":
    main()
