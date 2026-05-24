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
from typing import Any, Dict, List, Optional, Set

import click
import yaml

from .manifest import NoManifestFound, find_addons, read_manifest
from .nexterp_discover_scenarios import discover_scenarios, merge_scenarios


SCREENSHOTS_FILE = "screenshots.yaml"
DEFAULT_TIMEOUT_MS = 15000
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
# Marker comment we drop into auto-generated PNGs (in the tEXt chunk)
# so subsequent runs know the file was last touched by us, not a human.
# A PNG without this marker is treated as a manual override and never
# overwritten by the tool.
PNG_GENERATOR_MARKER = b"nexterp-gen-screenshots"


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------

def _load_spec(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf8") as fh:
        return yaml.safe_load(fh) or {}


def _dump_spec(path: str, spec: Dict[str, Any]) -> None:
    """Write ``spec`` to ``path`` with stable, human-friendly formatting."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as fh:
        yaml.safe_dump(
            spec,
            fh,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=120,
        )


def _png_is_manual(path: str) -> bool:
    """True if a PNG at ``path`` exists and lacks our generator marker.

    PNGs we wrote contain the ``nexterp-gen-screenshots`` byte sequence
    in a tEXt chunk (added below in ``_stamp_png``). Anything without
    that marker is treated as a hand-crafted override and preserved
    across runs — overwriting it would lose work the developer
    intentionally put in place.
    """
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as fh:
            return PNG_GENERATOR_MARKER not in fh.read()
    except OSError:
        return False


def _stamp_png(path: str) -> None:
    """Append a tEXt chunk to a PNG so we can later identify it as ours.

    The PNG spec lets us insert ancillary chunks anywhere between IHDR
    and IEND. We append a tEXt chunk just before IEND with the keyword
    ``Generator`` and our marker. If anything goes wrong (file vanished,
    not a real PNG) we silently leave the file alone — the worst-case
    outcome is the next run treats it as manual and skips it, which is
    safer than the opposite.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return
        iend_pos = data.rfind(b"IEND")
        if iend_pos < 4:  # need room for the 4-byte length prefix
            return
        # iend chunk = 4-byte length + "IEND" + 4-byte CRC. Insert before length.
        insert_at = iend_pos - 4
        keyword = b"Generator"
        text = PNG_GENERATOR_MARKER
        chunk_data = keyword + b"\x00" + text
        import struct
        import zlib
        length = struct.pack(">I", len(chunk_data))
        chunk_type = b"tEXt"
        crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
        chunk = length + chunk_type + chunk_data + crc
        new_data = data[:insert_at] + chunk + data[insert_at:]
        with open(path, "wb") as fh:
            fh.write(new_data)
    except OSError:
        return


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
        # Honor manual overrides: a PNG without our generator marker
        # was put there by hand; never overwrite it.
        if _png_is_manual(out):
            return  # silently skip; explicit log is at the run summary
        selector = step.get("selector")
        if selector:
            page.locator(selector).screenshot(path=out, timeout=timeout)
        else:
            page.screenshot(path=out, full_page=step.get("full_page", False))
        _stamp_png(out)
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
@click.option("--db", default="",
              help="Database name to log into. Required unless --discover-only is set.")
@click.option("--user", default="admin", help="Login user.")
@click.option("--password", default="admin", help="Login password.")
@click.option("--headless/--headed", default=True,
              help="Run Chromium headless. Use --headed when debugging selectors.")
@click.option("--viewport-width", type=int, default=DEFAULT_VIEWPORT["width"])
@click.option("--viewport-height", type=int, default=DEFAULT_VIEWPORT["height"])
@click.option(
    "--discover/--no-discover",
    default=False,
    help=(
        "Before running, walk each addon's views/wizards/static/src and "
        "auto-populate readme/screenshots.yaml with draft scenarios for "
        "every act_window, wizard and OWL component found. Scenarios "
        "whose name already exists in the file are preserved untouched."
    ),
)
@click.option(
    "--discover-only",
    is_flag=True,
    default=False,
    help="Run discovery and write the YAML files, then exit — no browser.",
)
def main(addon_dirs, addons_dir, odoo_url, db, user, password,
         headless, viewport_width, viewport_height,
         discover, discover_only):
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

    # --discover / --discover-only: walk each addon's source and merge
    # auto-emitted scenarios into its YAML before the run. Manual
    # scenarios (same `name` already in file) are preserved as-is.
    if discover or discover_only:
        for addon_name, addon_dir, _manifest in addons:
            spec_path = os.path.join(addon_dir, "readme", SCREENSHOTS_FILE)
            existing = _load_spec(spec_path)
            drafts = discover_scenarios(addon_name, addon_dir)
            new_spec, added = merge_scenarios(existing, drafts)
            if added or existing is None:
                _dump_spec(spec_path, new_spec)
                marker = "+" if existing is None else "~"
                click.echo(
                    f"{marker} {addon_name}: wrote {len(added)} new scenario(s) "
                    f"to readme/{SCREENSHOTS_FILE}"
                )
        if discover_only:
            return

    if not db:
        click.echo(
            "--db is required when not using --discover-only.", err=True
        )
        sys.exit(2)

    # Pre-filter to addons that actually declare scenarios; saves a
    # browser launch when nothing is to be done.
    to_process = []
    for addon_name, addon_dir, manifest in addons:
        spec_path = os.path.join(addon_dir, "readme", SCREENSHOTS_FILE)
        spec = _load_spec(spec_path)
        if spec and spec.get("scenarios"):
            # Drop scenarios with empty steps (e.g. OWL placeholders the
            # developer hasn't filled in yet) so they don't show as
            # spurious "ok" results.
            spec = dict(spec)
            spec["scenarios"] = [
                s for s in spec["scenarios"] if (s.get("steps") or [])
            ]
            if spec["scenarios"]:
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
