# License AGPLv3 (https://www.gnu.org/licenses/agpl-3.0-standalone.html)
# Copyright (c) 2026 NextERP Romania SRL
"""Auto-generate ``readme/{DESCRIPTION,USAGE,CONFIGURE,CONTEXT}.md`` from addon source.

Manual command (not a pre-commit hook — runs hit the Anthropic API and
take many seconds per addon):

    nexterp-gen-addon-readme-content --addons-dir=.

For each addon under ``--addons-dir`` that is missing one or more of the
four target files, this tool reads the addon's manifest + models +
views + controllers + hooks, hands the digested source to the Claude
API (Sonnet 4.6 by default), and writes the missing fragments to
``readme/``.

Idempotent: existing files are never overwritten unless ``--force`` is
passed. The next pre-commit pass of ``oca-gen-addon-readme`` picks the
new fragments into the assembled ``README.rst``, and
``nexterp-gen-addon-index-html`` propagates them to the branded
landing page.

Prompt caching is enabled on the (large, stable) system prompt so a
batch run over many addons reuses the same cached prefix across calls.

Cost guardrails: the per-addon code summary is capped at ``MAX_CODE_CHARS``
characters; larger addons get their model/view dumps truncated with a
marker so the LLM sees a representative sample. Set ``ANTHROPIC_API_KEY``
in the environment before running.
"""
import ast
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import click

try:
    import anthropic
except ImportError:
    anthropic = None

from lxml import etree

from .manifest import NoManifestFound, find_addons, read_manifest

# ---------------------------------------------------------------------------
# Constants

TARGET_FILES = ("DESCRIPTION.md", "USAGE.md", "CONFIGURE.md", "CONTEXT.md")

# Soft cap on the per-addon code summary (chars). Roughly ~25K tokens
# at 4 chars/token — leaves room for the system prompt + output within
# Sonnet 4.6's window without blowing the budget per call.
MAX_CODE_CHARS = 80_000

# Per-section cap inside the summary (models / views / controllers).
# Prevents one giant models/ directory from starving everything else.
SECTION_CAP_CHARS = 30_000

DEFAULT_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# System prompt — large, stable, cached on the API side.

GOLD_STANDARD_SGR = '''### DESCRIPTION.md
# Romania - SGR Base

Base module for the Romanian SGR (Sistem Garanție-Returnare / RetuRO)
deposit-return system in Odoo, retailer-oriented out of the box.

The 0.50 RON deposit is modeled as a **separate, stockable product** (one
per RetuRO category) that companion modules attach automatically to every
sale, purchase, invoice, POS or manufacturing order line of a beverage
product subject to SGR.

## What this module provides

- **Five SGR categories** matching the RetuRO retailer view: Plastic Small,
  Plastic Large, Metal, Glass Small, Glass Large.
- **Historical rate tables** (`nexterp.sgr.category.rate`,
  `nexterp.sgr.handling.rate`) — deposit per category × effective date,
  plus handling fee (TG) per category × collection method.
- **Product-level fields**: `is_sgr_product` flags an SGR packaging item;
  `sgr_product_id` links a regular product to its SGR companion.
- **Per-warehouse SGR setup**: `sgr_collection_type` (RVM / Manual / HoReCa)
  and a dedicated `sgr_return_location_id` for collected empties.
- **Two stock-based reports**: *SGR Position* (consolidated picture) and
  *SGR Stock & Movements* (period breakdown).

### USAGE.md
# Usage

## Day-to-day flows

### Receiving a delivery from a beverage supplier

With `nexterp_sgr_purchase` installed, the standard purchase flow handles
SGR automatically:

1. Create a Purchase Order with one or more beverage products.
2. On adding each beverage line, an SGR child line appears underneath with
   the matching category and the current deposit rate.
3. Confirm and receive the goods. Both the beverage and its SGR packaging
   land in stock as separate stock moves.

### Selling a beverage

1. On a Sale Order or POS receipt, scanning/adding a beverage adds the
   SGR companion line automatically.
2. At invoicing, the customer invoice carries beverage + SGR as separate
   lines (SGR line non-taxable).

## Reports

- **SGR Position** (*Inventory → Reports → SGR → SGR Position*) shows the
  consolidated SGR financial picture: value blocked in stock, receivable
  from RetuRO, owed to consumers.

  ![SGR Position consolidated report](./sgr_position_report.png)

### CONFIGURE.md
# Configuration

After installing `nexterp_sgr`, follow the steps below.

## 1. Company-wide default

Go to **Settings → Inventory → Warehouse → SGR** and choose whether new
documents auto-add SGR lines.

## 2. SGR Liability Account (if `nexterp_sgr_account` is installed)

Go to **Settings → Accounting → Fiscal Localization → SGR Liability Account**
and pick the account that should hold deposits. For Romania, the typical
choice is **462 "Creditori diverşi"**.

## 3. Warehouse setup

For each retail location, open
**Inventory → Configuration → Warehouses**, and set:

- **SGR Collection Type** — `RVM (Automated)`, `Manual` or `HoReCa`.
- **SGR Return Location** — a dedicated internal sub-location.

![SGR Categories list](./sgr_categories_list.png)

### CONTEXT.md
# Key features

- **Deposit modeled as a stockable product** — one separate product per
  RetuRO category, so the 0.50 RON deposit moves through your books as
  inventory rather than revenue.
- **Five pre-loaded RetuRO categories** with historical deposit rate
  tables.
- **Per-warehouse SGR setup** — pick *RVM / Manual / HoReCa* collection
  type and a dedicated *SGR Returns* internal location.
- **Companion modules** for each channel: `_sale`, `_purchase`,
  `_account`, `_pos`, `_mrp`, `_returo`. Install only what your retail
  mix needs.
- **Romanian first, but localization-agnostic** — RON-denominated by
  default, but values stored per category so the same architecture can
  be reused under other deposit-return schemes.
'''


SYSTEM_PROMPT = f"""You are a technical writer producing OCA-style README \
fragments for NextERP Romania Odoo modules.

You will receive a digested view of one Odoo addon's source code — its
manifest, model class definitions with fields, view XML records, \
controllers and post-init hooks. Your job is to produce up to four \
markdown fragments that document the module from a user-facing perspective:

- **DESCRIPTION.md** — what the module does, the business problem it solves,
  and a bulleted "What this module provides" list of its concrete \
  capabilities. Lead with a heading using the human-readable name from \
  the manifest. Aim for 200–500 words; longer is fine for complex modules.

- **USAGE.md** — end-user workflows: "How do I X?" recipes. Use numbered
  steps for each flow. Include reference to **menu paths** (e.g.
  *Inventory → Reports → SGR*) where the user clicks. If you reference a
  screenshot, use the form `![desc](./filename.png)` with a plausible
  filename — the screenshot tool generates PNGs in `static/description/`
  later. Skip USAGE entirely (return empty string) if the module is a
  pure backend extension with no user-visible workflow.

- **CONFIGURE.md** — numbered configuration steps the admin runs after
  install. Reference menu paths and the specific fields/settings to set.
  Skip (empty string) if the module needs zero configuration beyond
  installation.

- **CONTEXT.md** — bullet list of "Key features" suitable for an Odoo
  Apps store landing page sidebar. 4–10 bullets, each ≤ 2 lines.

## Style and constraints

- **English**, Markdown, OCA convention.
- Use precise references to **actual identifiers from the code**: model
  names (`account.move`), field names (`x_sgr_category_id`), method
  names, XML record IDs, menu labels. Do not invent identifiers.
- Use Romanian only for legal/accounting terms that are Romanian-specific
  (e.g. account names like "Creditori diverşi", "ANAF", "e-Factura").
- Mention dependencies (`depends` from manifest) only when they affect
  user workflow — not as a bare list.
- Use tables when listing multiple related items (e.g. companion
  modules, configuration options).
- Do not include CHANGELOG / HISTORY content — that's auto-generated
  separately. Do not include CONTRIBUTORS, INSTALL, ROADMAP — those are
  hand-curated.
- Do not start with "This module …" or "The purpose of this module …".
  Lead with the heading then a direct statement.
- Do not include preamble like "Here is the DESCRIPTION.md:". Return
  raw markdown content per the JSON schema.

## Gold-standard example (the `nexterp_sgr` module)

Here is the gold-standard README produced for `nexterp_sgr`. Match this \
style, level of detail, and structure for the module you receive next.

{GOLD_STANDARD_SGR}

## Output

Return a JSON object with exactly the keys the user requests (a subset
of `DESCRIPTION`, `USAGE`, `CONFIGURE`, `CONTEXT`). For files you are
asked to skip (because the module does not warrant them — e.g. a
backend-only module with no USAGE), return an empty string for that
key.
"""

# ---------------------------------------------------------------------------
# Code summarization — turn an addon directory into a compact text blob

_FIELD_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*fields\.([A-Z][A-Za-z0-9_]*)\((.*)$"
)


def _truncate(text: str, cap: int, label: str) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n\n... [truncated {len(text) - cap} chars of {label}]\n"


def _summarize_py_models(models_dir: Path) -> str:
    """Walk models/ (or wizards/, controllers/) and return a compact text
    summary: class names, _name / _inherit / _description, fields with
    string=/help=, and method names with first docstring line."""
    if not models_dir.is_dir():
        return ""
    parts: List[str] = []
    for py_file in sorted(models_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = py_file.relative_to(models_dir.parent)
        file_parts: List[str] = [f"### {rel}"]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            class_lines = [f"class {node.name}:"]
            for stmt in node.body:
                # _name / _inherit / _description assignments
                if isinstance(stmt, ast.Assign):
                    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                        continue
                    tname = stmt.targets[0].id
                    if tname in ("_name", "_inherit", "_description", "_order"):
                        try:
                            val = ast.literal_eval(stmt.value)
                        except (ValueError, SyntaxError):
                            val = "?"
                        class_lines.append(f"    {tname} = {val!r}")
                        continue
                    # field assignments
                    val = stmt.value
                    if (
                        isinstance(val, ast.Call)
                        and isinstance(val.func, ast.Attribute)
                        and isinstance(val.func.value, ast.Name)
                        and val.func.value.id == "fields"
                    ):
                        kind = val.func.attr
                        kwargs = []
                        for kw in val.keywords:
                            if kw.arg in ("string", "help", "comodel_name", "selection", "related", "compute"):
                                try:
                                    kv = ast.literal_eval(kw.value)
                                except (ValueError, SyntaxError):
                                    kv = "<expr>"
                                if isinstance(kv, str) and len(kv) > 120:
                                    kv = kv[:117] + "..."
                                kwargs.append(f"{kw.arg}={kv!r}")
                        # positional first arg of Many2one/Many2many is comodel_name
                        if val.args and kind in ("Many2one", "Many2many", "One2many"):
                            try:
                                first = ast.literal_eval(val.args[0])
                                kwargs.insert(0, f"-> {first}")
                            except (ValueError, SyntaxError):
                                pass
                        class_lines.append(
                            f"    {tname} = fields.{kind}({', '.join(kwargs)})"
                        )
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(stmt) or ""
                    first = doc.splitlines()[0].strip() if doc else ""
                    decorators = [
                        ast.unparse(d).split("(")[0] if hasattr(ast, "unparse") else ""
                        for d in stmt.decorator_list
                    ]
                    deco = " ".join(f"@{d}" for d in decorators if d) + " " if decorators else ""
                    sig = f"{deco}def {stmt.name}(...)"
                    if first:
                        sig += f"  # {first[:120]}"
                    class_lines.append(f"    {sig}")
            if len(class_lines) > 1:
                file_parts.append("\n".join(class_lines))
        if len(file_parts) > 1:
            parts.append("\n\n".join(file_parts))
    return "\n\n".join(parts)


def _summarize_xml_views(addon_dir: Path) -> str:
    """Extract <record id="..." model="..."> entries plus menus from view XML."""
    parts: List[str] = []
    for sub in ("views", "wizards", "data", "report", "security"):
        d = addon_dir / sub
        if not d.is_dir():
            continue
        for xml_file in sorted(d.rglob("*.xml")):
            try:
                source = xml_file.read_bytes()
                root = etree.fromstring(source)
            except (OSError, etree.XMLSyntaxError):
                continue
            entries: List[str] = []
            for rec in root.iter("record"):
                model = rec.get("model", "")
                rid = rec.get("id", "")
                # Try to extract a name / string field for context
                name_node = rec.find("./field[@name='name']")
                name = (name_node.text or "").strip() if name_node is not None else ""
                if name:
                    entries.append(f"- record id={rid} model={model} name={name!r}")
                else:
                    entries.append(f"- record id={rid} model={model}")
            for menu in root.iter("menuitem"):
                mid = menu.get("id", "")
                mname = menu.get("name", "")
                parent = menu.get("parent", "")
                action = menu.get("action", "")
                entries.append(
                    f"- menuitem id={mid} name={mname!r} parent={parent} action={action}"
                )
            if entries:
                rel = xml_file.relative_to(addon_dir)
                parts.append(f"### {rel}\n" + "\n".join(entries))
    return "\n\n".join(parts)


def _summarize_existing_readme(addon_dir: Path) -> str:
    """Pull README.rst and any existing readme/*.md so the model can stay
    consistent with content the developer already wrote."""
    chunks: List[str] = []
    rrst = addon_dir / "README.rst"
    if rrst.is_file():
        try:
            text = rrst.read_text(encoding="utf-8")
            if text.strip():
                chunks.append(f"### Existing README.rst\n{text[:4000]}")
        except (OSError, UnicodeDecodeError):
            pass
    readme_dir = addon_dir / "readme"
    if readme_dir.is_dir():
        for md in sorted(readme_dir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if text.strip():
                chunks.append(f"### Existing readme/{md.name}\n{text}")
    return "\n\n".join(chunks)


def _manifest_summary(manifest: Dict) -> str:
    """Compact view of the manifest fields that matter for documentation."""
    keep = (
        "name",
        "summary",
        "version",
        "category",
        "license",
        "application",
        "depends",
        "external_dependencies",
        "post_init_hook",
    )
    return "\n".join(
        f"- {k}: {manifest[k]!r}" for k in keep if k in manifest
    )


def gather_addon_context(addon_dir: Path, manifest: Dict) -> str:
    """Build the per-addon source digest passed to the model."""
    sections: List[Tuple[str, str]] = [
        ("MANIFEST", _manifest_summary(manifest)),
        ("MODELS", _truncate(_summarize_py_models(addon_dir / "models"), SECTION_CAP_CHARS, "models")),
        ("WIZARDS", _truncate(_summarize_py_models(addon_dir / "wizards"), SECTION_CAP_CHARS, "wizards")),
        ("CONTROLLERS", _truncate(_summarize_py_models(addon_dir / "controllers"), SECTION_CAP_CHARS, "controllers")),
        ("VIEWS_AND_DATA", _truncate(_summarize_xml_views(addon_dir), SECTION_CAP_CHARS, "views")),
    ]
    # hooks.py is often a single file at the addon root
    hooks = addon_dir / "hooks.py"
    if hooks.is_file():
        try:
            sections.append(("HOOKS", hooks.read_text(encoding="utf-8")[:4000]))
        except (OSError, UnicodeDecodeError):
            pass
    existing = _summarize_existing_readme(addon_dir)
    if existing:
        sections.append(("EXISTING_README", existing))
    blob = "\n\n".join(f"## {label}\n\n{body}" for label, body in sections if body)
    return _truncate(blob, MAX_CODE_CHARS, "total addon")


# ---------------------------------------------------------------------------
# LLM call

def _missing_files(readme_dir: Path) -> List[str]:
    return [
        f.replace(".md", "")
        for f in TARGET_FILES
        if not (readme_dir / f).is_file()
    ]


def _build_schema(missing: List[str]) -> Dict:
    """Build a JSON schema requiring exactly the missing-file keys."""
    return {
        "type": "object",
        "properties": {k: {"type": "string"} for k in missing},
        "required": missing,
        "additionalProperties": False,
    }


def call_llm(
    client,
    addon_name: str,
    code_digest: str,
    missing: List[str],
    model: str,
) -> Tuple[Dict[str, str], object]:
    """One streaming call; returns (parsed_json, usage)."""
    user = (
        f"# Module to document: `{addon_name}`\n\n"
        f"Generate ONLY these files: {', '.join(missing)}.\n"
        f"Return a JSON object with exactly these keys.\n\n"
        f"## Source digest\n\n{code_digest}"
    )
    schema = _build_schema(missing)
    with client.messages.stream(
        model=model,
        max_tokens=12_000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
        messages=[{"role": "user", "content": user}],
        output_config={
            "format": {"type": "json_schema", "schema": schema},
            "effort": "medium",
        },
        thinking={"type": "adaptive"},
    ) as stream:
        message = stream.get_final_message()
    text = next(b.text for b in message.content if b.type == "text")
    return json.loads(text), message.usage


# ---------------------------------------------------------------------------
# Per-addon driver

def process_addon(
    client,
    addon_name: str,
    addon_dir: Path,
    manifest: Dict,
    force: bool,
    dry_run: bool,
    model: str,
) -> Optional[object]:
    """Generate and write missing files for one addon. Returns usage or None."""
    readme_dir = addon_dir / "readme"
    readme_dir.mkdir(exist_ok=True)
    if force:
        missing = [f.replace(".md", "") for f in TARGET_FILES]
    else:
        missing = _missing_files(readme_dir)
    if not missing:
        click.echo(f"  {addon_name}: all readme files present — skipping")
        return None

    code_digest = gather_addon_context(addon_dir, manifest)
    click.echo(
        f"  {addon_name}: generating {', '.join(missing)} "
        f"({len(code_digest):,} chars of context)"
    )

    if dry_run:
        click.echo(f"    (dry-run — no API call, no files written)")
        return None

    try:
        result, usage = call_llm(client, addon_name, code_digest, missing, model)
    except anthropic.APIError as exc:
        click.echo(f"    LLM call failed: {exc}", err=True)
        return None

    for key in missing:
        content = (result.get(key) or "").strip()
        target = readme_dir / f"{key}.md"
        if not content:
            click.echo(f"    {key}: skipped by model (empty) — not writing")
            continue
        target.write_text(content + "\n", encoding="utf-8")
        click.echo(f"    wrote {target.relative_to(addon_dir)}")

    return usage


# ---------------------------------------------------------------------------
# CLI

@click.command()
@click.option(
    "--addons-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    default=".",
    show_default=True,
    help="Root directory containing one or more Odoo addons (each with a __manifest__.py).",
)
@click.option(
    "--addon-dir",
    "addon_dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Restrict to one or more specific addon directories. Repeatable.",
)
@click.option(
    "--force/--no-force",
    default=False,
    help="Regenerate even when target files already exist (overwrites).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would be generated without calling the API.",
)
@click.option(
    "--model",
    default=DEFAULT_MODEL,
    show_default=True,
    help="Anthropic model ID.",
)
def main(addons_dir, addon_dirs, force, dry_run, model):
    """Generate readme/{DESCRIPTION,USAGE,CONFIGURE,CONTEXT}.md from addon source."""
    if not dry_run:
        if anthropic is None:
            click.echo(
                "anthropic SDK not installed — run `pip install anthropic`.",
                err=True,
            )
            sys.exit(2)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            click.echo(
                "ANTHROPIC_API_KEY not set — export it before running, or use --dry-run.",
                err=True,
            )
            sys.exit(2)

    client = None if dry_run else anthropic.Anthropic()

    # Resolve addon set
    if addon_dirs:
        targets = []
        for d in addon_dirs:
            d = Path(d).resolve()
            try:
                manifest = read_manifest(str(d))
            except NoManifestFound:
                click.echo(f"  {d}: no manifest — skipping", err=True)
                continue
            targets.append((d.name, str(d), manifest))
    else:
        targets = list(find_addons(addons_dir))

    if not targets:
        click.echo("No addons found.")
        return

    click.echo(f"Found {len(targets)} addon(s).")

    total_input = total_output = total_cached_read = total_cached_write = 0
    processed = 0
    start = time.time()

    for addon_name, addon_dir, manifest in targets:
        if not manifest.get("installable", True):
            continue
        usage = process_addon(
            client,
            addon_name,
            Path(addon_dir),
            manifest,
            force=force,
            dry_run=dry_run,
            model=model,
        )
        if usage is not None:
            total_input += usage.input_tokens
            total_output += usage.output_tokens
            total_cached_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            total_cached_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
            processed += 1

    elapsed = time.time() - start
    click.echo(
        f"\nDone in {elapsed:.1f}s. {processed} addon(s) processed.\n"
        f"  input tokens:        {total_input:,}\n"
        f"  output tokens:       {total_output:,}\n"
        f"  cache read tokens:   {total_cached_read:,}\n"
        f"  cache write tokens:  {total_cached_write:,}"
    )


if __name__ == "__main__":
    main()
