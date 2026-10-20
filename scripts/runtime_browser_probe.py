#!/usr/bin/env python3
"""Trusted fixture browser half; the parent performs privileged lifecycle actions."""

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError("O navegador deve executar sem root.")
    config = json.loads(args.config.read_text())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True
        )
        try:
            anonymous = browser.new_context()
            denied = anonymous.new_page()
            assert denied.goto("http://" + config["routes"][0]["host"]).status == 403
            context = browser.new_context(accept_downloads=True)
            pages, observed = [], []
            for route in config["routes"]:
                context.add_cookies(
                    [
                        {
                            "name": "shinyd_probe",
                            "value": config["token"],
                            "url": "http://" + route["host"],
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                page = context.new_page()
                page.goto("http://" + route["host"])
                page.wait_for_function(
                    "document.querySelector('#state')?.textContent.includes('|')"
                )
                state = page.locator("#state").inner_text().split("|")
                assert state[0] == route["id"]
                assert page.locator("#credential_boundary").inner_text() == "credential-boundary-ok"
                observed.append({"id": state[0], "r": state[1], "shiny": state[2], "pid": state[3]})
                page.get_by_role("button", name="Somar", exact=True).click()
                page.wait_for_function(
                    "document.querySelector('#state').textContent.endsWith('|1')"
                )
                pages.append(page)
            pages[0].locator("input[type=file]").set_input_files(
                {"name": "probe.txt", "mimeType": "text/plain", "buffer": b"unix-relay-upload"}
            )
            pages[0].wait_for_function(
                "document.querySelector('#uploaded').textContent === 'unix-relay-upload'"
            )
            with pages[0].expect_download() as download:
                pages[0].get_by_role("link", name="Baixar estado").click()
            assert Path(download.value.path()).read_text().strip().endswith("|1")
            print(json.dumps({"stage": "stop_first", "observed": observed}), flush=True)
            assert input() == "stopped"
            pages[1].get_by_role("button", name="Somar", exact=True).click()
            pages[1].wait_for_function(
                "document.querySelector('#state').textContent.endsWith('|2')"
            )
            print(json.dumps({"stage": "restart_executor"}), flush=True)
            assert input() == "restarted"
            pages[1].reload()
            pages[1].wait_for_function(
                "document.querySelector('#state')?.textContent.includes('|')"
            )
            state = pages[1].locator("#state").inner_text().split("|")
            assert state[0] == observed[1]["id"] and state[3] == observed[1]["pid"]
            assert state[-1] == "0"  # Relay restart closes old WebSocket/session.
            pages[1].get_by_role("button", name="Somar", exact=True).click()
            pages[1].wait_for_function(
                "document.querySelector('#state').textContent.endsWith('|1')"
            )
            print(json.dumps({"stage": "passed"}), flush=True)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
