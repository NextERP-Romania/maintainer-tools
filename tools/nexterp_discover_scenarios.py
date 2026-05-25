# License AGPLv3 (https://www.gnu.org/licenses/agpl-3.0-standalone.html)
# Copyright (c) 2026 NextERP Romania SRL
"""Discover Playwright scenarios from an Odoo addon's source tree.

Drives the ``--discover`` mode of ``nexterp-gen-screenshots``. Given an
addon directory, it inspects three sources of "things worth a
screenshot" and emits a YAML *draft* that the developer can prune and
refine:

1. **Actions** — every ``<record model="ir.actions.act_window">`` in
   ``data/*.xml`` and ``views/*.xml``. Each becomes a ``goto`` scenario
   targeting ``/odoo/action-<addon>.<xml_id>`` plus a screenshot.
2. **Wizards** — same record type but with ``target="new"``. Treated
   like actions, but the screenshot is scoped to ``.modal-dialog`` and
   the wait selector is ``.modal``.
3. **OWL components** — every ``.esm.js`` under ``static/src/`` (and
   its sibling ``.xml`` template). These cannot be auto-driven because
   their entry-point depends on the parent view, so we emit a
   *placeholder* scenario with TODO comments — the developer fills in
   the navigation steps once.

Merge semantics with an existing ``readme/screenshots.yaml``:

- Scenarios whose ``name`` already exists in the YAML are **never**
  touched (the developer's manual steps win).
- New scenarios are appended at the end of the ``scenarios`` list.
- The discovery comments mark which entries are auto-emitted so a
  reviewer can spot drafts at a glance.
"""
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from lxml import etree

# YAML is loaded by the caller; we work on Python dicts/lists.

ACTION_TAG = "ir.actions.act_window"

SOURCE_KIND = Tuple[str, str]  # (kind, file_path) — kind ∈ {"action", "wizard", "owl"}

WAIT_PRIMARY = ".o_form_view, .o_list_view, .o_kanban_view, .o_pivot_view, .o_graph_view"
WAIT_MODAL = ".modal-dialog"


# ---------------------------------------------------------------------------
# XML scanning
# ---------------------------------------------------------------------------

def _xml_files(addon_dir: str) -> List[str]:
    """All data-ish XML under the addon (views, wizards, data, reports)."""
    candidates: List[str] = []
    for sub in ("data", "views", "wizards", "wizard", "reports"):
        d = os.path.join(addon_dir, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.endswith(".xml"):
                    candidates.append(os.path.join(root, f))
    return candidates


def _parse_actions(xml_path: str) -> List[Dict[str, str]]:
    """Return the act_window records declared in ``xml_path``.

    Each dict has keys ``id`` (xml id), ``name`` (best-effort, may be
    empty if the manifest uses ``compute``/``related``), ``target``
    (``current`` or ``new``), and ``res_model``.
    """
    try:
        tree = etree.parse(xml_path)
    except etree.XMLSyntaxError:
        return []
    out: List[Dict[str, str]] = []
    for record in tree.iter("record"):
        if record.get("model") != ACTION_TAG:
            continue
        xml_id = record.get("id")
        if not xml_id:
            continue
        entry = {"id": xml_id, "name": "", "target": "current", "res_model": ""}
        for field in record.findall("field"):
            fname = field.get("name")
            if fname == "name":
                # Either the inline text or a child eval/ref.
                txt = (field.text or "").strip()
                if txt:
                    entry["name"] = txt
            elif fname == "target":
                entry["target"] = (field.text or "current").strip()
            elif fname == "res_model":
                entry["res_model"] = (field.text or "").strip()
        out.append(entry)
    return out


def _scan_actions(addon_dir: str) -> Tuple[List[Dict], List[Dict]]:
    """Walk the addon and split discovered actions into (regular, wizards).

    Wizards are recognized by ``target="new"`` (modal) AND by the file
    living under ``wizards/`` or ``wizard/``. Either signal is enough,
    so a regular act_window placed under ``wizards/`` is still treated
    as a wizard for screenshotting purposes.
    """
    regular: List[Dict] = []
    wizards: List[Dict] = []
    for xml_path in _xml_files(addon_dir):
        rel = os.path.relpath(xml_path, addon_dir)
        is_wizard_dir = rel.startswith(("wizards", "wizard"))
        for action in _parse_actions(xml_path):
            action["_source"] = rel
            if action.get("target") == "new" or is_wizard_dir:
                wizards.append(action)
            else:
                regular.append(action)
    return regular, wizards


# ---------------------------------------------------------------------------
# OWL component scanning
# ---------------------------------------------------------------------------

OWL_TEMPLATE_RE = re.compile(r't-name=["\']([^"\']+)["\']')


def _scan_owl_components(addon_dir: str) -> List[Dict[str, str]]:
    """Return one entry per ``.esm.js`` file under ``static/src/``.

    Returns dicts ``{file, template, name, slug}``. ``template`` is the
    first ``t-name="..."`` found in a sibling .xml file (best-effort —
    OWL templates can be defined elsewhere too). ``slug`` is a snake-
    cased identifier safe to use in YAML keys.
    """
    src_dir = os.path.join(addon_dir, "static", "src")
    if not os.path.isdir(src_dir):
        return []
    out: List[Dict[str, str]] = []
    for root, _dirs, files in os.walk(src_dir):
        for f in files:
            if not f.endswith(".esm.js") and not f.endswith(".js"):
                continue
            js_path = os.path.join(root, f)
            base = f.rsplit(".", 1)[0]
            xml_sibling = os.path.join(root, base + ".xml")
            template = ""
            if os.path.exists(xml_sibling):
                with open(xml_sibling, "r", encoding="utf8") as fh:
                    m = OWL_TEMPLATE_RE.search(fh.read())
                    if m:
                        template = m.group(1)
            rel = os.path.relpath(js_path, addon_dir)
            # slug = parent dir(s) + filename, lowered
            slug = (
                re.sub(r"[^a-z0-9]+", "_", rel.lower())
                .strip("_")
                .replace("static_src_", "")
                .replace("_esm_js", "")
                .replace("_js", "")
            )
            out.append(
                {
                    "file": rel,
                    "template": template,
                    "name": template or base,
                    "slug": slug,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Scenario emission
# ---------------------------------------------------------------------------

def _action_scenario(
    addon_name: str, action: Dict[str, str], is_wizard: bool
) -> Dict:
    """Build a scenario dict for a single action / wizard."""
    xml_id = action["id"]
    qualified = f"{addon_name}.{xml_id}"
    name_slug = f"wizard_{xml_id}" if is_wizard else f"action_{xml_id}"
    display = action.get("name") or xml_id
    if is_wizard:
        steps = [
            {"goto": f"/odoo/action-{qualified}"},
            {"wait": ".modal-dialog"},
            {"sleep": 400},
            {
                "screenshot": {
                    "selector": ".modal-dialog",
                    "filename": f"{name_slug}.png",
                }
            },
        ]
    else:
        steps = [
            {"goto": f"/odoo/action-{qualified}"},
            {"wait": WAIT_PRIMARY},
            {"sleep": 600},
            {"screenshot": {"filename": f"{name_slug}.png"}},
        ]
    return {
        "name": name_slug,
        "description": (
            f"{'Wizard' if is_wizard else 'Action'}: {display} (auto-discovered)"
        ),
        "steps": steps,
    }


def _owl_scenario(component: Dict[str, str]) -> Dict:
    """Build a placeholder scenario for an OWL component.

    OWL components don't have a navigable URL — they render inside a
    parent view triggered by some user action (clicking a button,
    opening a session, etc.). We emit a scenario with the steps left
    *commented out* and a TODO line, so the run-time tool skips it
    (no actionable steps) but the developer sees a starting point.
    """
    slug = component["slug"]
    return {
        "name": f"owl_{slug}",
        "description": (
            f"OWL component: {component['name']} "
            f"(file: {component['file']}) — TODO: define steps"
        ),
        "steps": [
            # All commented out — leave the developer to wire navigation.
            # An empty steps list is treated as a no-op by the runner.
        ],
    }


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def discover_scenarios(addon_name: str, addon_dir: str) -> List[Dict]:
    """Return the full list of draft scenarios for ``addon_dir``."""
    regular, wizards = _scan_actions(addon_dir)
    owl = _scan_owl_components(addon_dir)
    out: List[Dict] = []
    for action in regular:
        out.append(_action_scenario(addon_name, action, is_wizard=False))
    for wizard in wizards:
        out.append(_action_scenario(addon_name, wizard, is_wizard=True))
    for comp in owl:
        out.append(_owl_scenario(comp))
    return out


def merge_scenarios(
    existing_spec: Optional[Dict],
    discovered: List[Dict],
) -> Tuple[Dict, List[str]]:
    """Merge ``discovered`` into ``existing_spec``.

    Returns ``(new_spec, added_names)``. Existing scenarios are never
    modified or reordered; new ones are appended at the end.
    """
    spec: Dict = dict(existing_spec or {})
    existing_scenarios: List[Dict] = list(spec.get("scenarios") or [])
    existing_names: Set[str] = {s.get("name") for s in existing_scenarios}

    added: List[str] = []
    for scenario in discovered:
        if scenario["name"] in existing_names:
            continue
        existing_scenarios.append(scenario)
        existing_names.add(scenario["name"])
        added.append(scenario["name"])

    spec["scenarios"] = existing_scenarios
    spec.setdefault(
        "setup",
        {"viewport": {"width": 1440, "height": 900}, "pause_ms_default": 500},
    )
    return spec, added
