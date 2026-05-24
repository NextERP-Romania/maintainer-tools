# License AGPLv3 (https://www.gnu.org/licenses/agpl-3.0-standalone.html)
# Copyright (c) 2026 NextERP Romania SRL
"""Drive a real Odoo browser session to capture per-addon screenshots.

Manual command (not a pre-commit hook — runs are slow and require a
live Odoo at the URL passed via ``--odoo-url``):

    nexterp-gen-screenshots --addons-dir=. \\
        --odoo-url=http://localhost:1990 --db=odooapps \\
        --user=admin --password=admin

For each addon under ``--addons-dir`` that contains a
``readme/screenshots.yaml``, the tool logs in once, walks the addon's
scenarios sequentially, and writes the resulting PNGs to
``static/description/`` (so they're picked up by both the OCA-style
README.rst and our branded index.html). Scenarios that fail (selector
timeout, navigation error) are reported and skipped; the rest of the
run continues, so a flaky scenario doesn't sink an entire batch.

YAML schema (kept intentionally small — see template/module/readme/
screenshots.yaml.example for a richer reference):

    setup:                              # optional, applies to whole file
      viewport: {width: 1440, height: 900}
      install_modules: [<addon>]        # use `odoo-bin --update` outside
                                         # this tool; we only screenshot
      language: en_US
      pause_ms_default: 600             # debounce after each step
    scenarios:
      - name: <scenario_slug>
        description: <human readable>
        steps:
          - goto: /odoo/settings
          - wait: .o_action_manager
          - fill:
              selector: input[type=search]
              value: SGR
          - click: text=SGR
          - screenshot: settings_sgr.png            # full page
          - screenshot:
              selector: .o_settings_block:has-text("SGR")
              filename: settings_sgr_block.png      # element-scoped

Steps are processed in order. Each accepts either a string shorthand
(``click: text=Confirm``) or a mapping with extra keys (timeout, etc).
"""
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import click
import yaml

from .manifest import NoManifestFound, find_addons, read_manifest


SCREENSHOTS_FILE = "screenshots.yaml"
DEFAULT_TIMEOUT_MS = 15000
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------

def _load_spec(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf8") as fh:
        return yaml.safe_load(fh) or {}


def _normalize_step(step: Any) -> Dict[str, Any]:
    """Accept either ``{action: arg}`` shorthand or full mapping.

    Examples:
        ``goto: /odoo/settings`` → {action: "goto", value: "/odoo/settings"}
        ``screenshot: foo.png`` → {action: "screenshot", filename: "foo.png"}
        ``screenshot: {selector: ..., filename: ...}`` → mapping as-is
    """
    if not isinstance(step, dict) or len(step) != 1:
        # Already explicit (multi-key dict — keep as-is)
        return step if isinstance(step, dict) else {"action": str(step)}
    action, arg = next(iter(step.items()))
    if isinstance(arg, dict):
        return {"action": action, **arg}
    return {"action": action, "value": arg}


# ---------------------------------------------------------------------------
# Playwright session
# ---------------------------------------------------------------------------

class OdooSession:
    """Thin wrapper that hides Playwright bookkeeping behind a small API.

    A single browser is reused across all addons and scenarios — Odoo
    cold-loads its assets on the first navigation, so reusing the page
    cuts overall runtime roughly in half on a 20-addon batch.
    """

    def __init__(self, odoo_url: str, db: str, user: str, password: str,
                 headless: bool, viewport: Dict[str, int]):
        self.odoo_url = odoo_url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self.headless = headless
        self.viewport = viewport
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self):
        # Local import keeps this module importable when only the
        # *other* generators are needed (CI/pre-commit on machines
        # without Chromium).
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            viewport=self.viewport,
            device_scale_factor=1,
        )
        self.page = self._context.new_page()
        self._login()
        return self

    def __exit__(self, *exc):
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _login(self):
        page = self.page
        page.goto(f"{self.odoo_url}/web/login", timeout=DEFAULT_TIMEOUT_MS)
        # On a fresh container there may be a database-selector page
        # first; pick the right DB if we land on it.
        if "/web/database/selector" in page.url:
            page.click(f'a:has-text("{self.db}")')
            page.wait_for_url("**/web/login*", timeout=DEFAULT_TIMEOUT_MS)
        page.fill('input[name="login"]', self.user)
        page.fill('input[name="password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_url(
            "**/odoo**",
            timeout=DEFAULT_TIMEOUT_MS,
        )


# ---------------------------------------------------------------------------
# Step executor
# ---------------------------------------------------------------------------

def _run_step(session: OdooSession, step: Dict[str, Any],
              output_dir: str, default_pause_ms: int) -> None:
    """Execute one normalized step. Raises on unknown action."""
    page = session.page
    action = step.get("action")
    timeout = step.get("timeout_ms", DEFAULT_TIMEOUT_MS)

    if action == "goto":
        url = step["value"]
        if url.startswith("/"):
            url = session.odoo_url + url
        page.goto(url, timeout=timeout)
    elif action == "wait":
        page.wait_for_selector(step["value"], timeout=timeout)
    elif action == "wait_url":
        page.wait_for_url(step["value"], timeout=timeout)
    elif action == "click":
        page.click(step["value"], timeout=timeout)
    elif action == "fill":
        page.fill(step["selector"], step["value"], timeout=timeout)
    elif action == "press":
        page.keyboard.press(step["value"])
    elif action == "scroll":
        # value = selector to scroll into view, else px (int)
        target = step["value"]
        if isinstance(target, int):
            page.mouse.wheel(0, target)
        else:
            page.locator(target).scroll_into_view_if_needed(timeout=timeout)
    elif action == "screenshot":
        filename = step.get("filename") or step.get("value")
        if not filename:
            raise ValueError("screenshot step needs filename or shorthand value")
        out = os.path.join(output_dir, filename)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        selector = step.get("selector")
        if selector:
            page.locator(selector).screenshot(path=out, timeout=timeout)
        else:
            page.screenshot(path=out, full_page=step.get("full_page", False))
    elif action == "sleep":
        # explicit sleep (ms) — rarely needed once selectors are right
        time.sleep(int(step["value"]) / 1000.0)
    else:
        raise ValueError(f"unknown step action: {action!r}")

    if default_pause_ms:
        page.wait_for_timeout(default_pause_ms)


# ---------------------------------------------------------------------------
# Per-addon driver
# ---------------------------------------------------------------------------

def run_addon_scenarios(session: OdooSession, addon_name: str, addon_dir: str,
                        spec: Dict[str, Any]) -> Dict[str, str]:
    """Execute every scenario declared in the addon's spec.

    Returns a ``{scenario_name: 'ok' | error_msg}`` mapping suitable for
    a CLI summary at the end of the run.
    """
    setup = spec.get("setup") or {}
    pause_ms = int(setup.get("pause_ms_default", 400))
    output_dir = os.path.join(addon_dir, "static", "description")

    results: Dict[str, str] = {}
    for sc in spec.get("scenarios") or []:
        name = sc.get("name", "<unnamed>")
        steps = [_normalize_step(s) for s in (sc.get("steps") or [])]
        try:
            for step in steps:
                _run_step(session, step, output_dir, pause_ms)
            results[name] = "ok"
        except Exception as e:  # noqa: BLE001 — best-effort batch run
            results[name] = f"FAIL: {type(e).__name__}: {e}"
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--addon-dir",
    "addon_dirs",
    type=click.Path(dir_okay=True, file_okay=False, exists=True),
    multiple=True,
    help="Addon directory to process. May be repeated.",
)
@click.option(
    "--addons-dir",
    type=click.Path(dir_okay=True, file_okay=False, exists=True),
    help=(
        "Directory containing several addons; every addon with a "
        "readme/screenshots.yaml is processed."
    ),
)
@click.option("--odoo-url", default="http://localhost:8069",
              help="Base URL of the running Odoo instance.")
@click.option("--db", required=True, help="Database name to log into.")
@click.option("--user", default="admin", help="Login user.")
@click.option("--password", default="admin", help="Login password.")
@click.option("--headless/--headed", default=True,
              help="Run Chromium headless. Use --headed when debugging selectors.")
@click.option("--viewport-width", type=int, default=DEFAULT_VIEWPORT["width"])
@click.option("--viewport-height", type=int, default=DEFAULT_VIEWPORT["height"])
def main(addon_dirs, addons_dir, odoo_url, db, user, password,
         headless, viewport_width, viewport_height):
    """Capture screenshots for each addon's readme/screenshots.yaml.

    The tool expects Odoo to already be running, the modules to be
    installed (with demo data if needed), and the user to have
    permission to reach every screen referenced in the scenarios.
    Bootstrapping the database is outside this tool's scope on purpose
    — keep it free of orchestration concerns.
    """
    addons = []
    seen = set()

    def _add(name, addon_dir, manifest):
        key = os.path.abspath(addon_dir)
        if key in seen:
            return
        seen.add(key)
        addons.append((name, addon_dir, manifest))

    if addons_dir:
        for name, addon_dir, manifest in find_addons(addons_dir):
            _add(name, addon_dir, manifest)
    for addon_dir in addon_dirs:
        addon_name = os.path.basename(os.path.abspath(addon_dir))
        try:
            manifest = read_manifest(addon_dir)
        except NoManifestFound:
            continue
        _add(addon_name, addon_dir, manifest)

    # Pre-filter to addons that actually declare scenarios; saves a
    # browser launch when nothing is to be done.
    to_process = []
    for addon_name, addon_dir, manifest in addons:
        spec_path = os.path.join(addon_dir, "readme", SCREENSHOTS_FILE)
        spec = _load_spec(spec_path)
        if spec and spec.get("scenarios"):
            to_process.append((addon_name, addon_dir, spec))

    if not to_process:
        click.echo("No readme/screenshots.yaml files with scenarios — nothing to do.")
        return

    viewport = {"width": viewport_width, "height": viewport_height}
    summary = []

    with OdooSession(odoo_url, db, user, password, headless, viewport) as session:
        for addon_name, addon_dir, spec in to_process:
            click.echo(f"\n▶ {addon_name}")
            results = run_addon_scenarios(session, addon_name, addon_dir, spec)
            for name, status in results.items():
                marker = "✓" if status == "ok" else "✗"
                click.echo(f"  {marker} {name}: {status}")
                summary.append((addon_name, name, status))

    failed = [s for s in summary if s[2] != "ok"]
    if failed:
        click.echo(f"\n{len(failed)} scenario(s) failed.", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
