# License AGPLv3 (https://www.gnu.org/licenses/agpl-3.0-standalone.html)
# Copyright (c) 2026 NextERP Romania SRL
"""Generate a NextERP-branded ``static/description/index.html`` per addon.

This is a separate command from ``oca-gen-addon-readme``: the OCA tool
continues to produce ``README.rst`` for GitHub (and, by default, a
Docutils-rendered ``index.html``), while this command writes a richer
HTML presentation page suitable for https://apps.odoo.com — with a hero,
CSS-only tabs (Overview / Features / Configuration / How it works) and a
shared "NextERP suite" section listing the other addons in the same
``--addons-dir``.

The shared NextERP brand chrome (suite cards, company footer, color
palette) lives entirely inside the Jinja template shipped with this
tool, so every addon picks up the same look without duplicating any
presentation copy. Module-specific copy is taken from the same
``readme/*.md`` fragments the OCA tool already reads (DESCRIPTION,
CONTEXT, CONFIGURE, USAGE), which keeps a single source of truth.

To avoid a tug-of-war with ``oca-gen-addon-readme`` (which writes
index.html only when the existing file contains its own marker), every
file produced here embeds ``nexterp-gen-addon-index-html`` and we treat
either marker as "owned by a generator". A file lacking both markers is
assumed hand-crafted and left untouched.
"""
import os
import re
from typing import Dict, List, Optional

import click
from jinja2 import Template
from markdown_it import MarkdownIt

from .manifest import NoManifestFound, find_addons, read_manifest

OUR_MARKER = "nexterp-gen-addon-index-html"
OCA_MARKER = "oca-gen-addon-readme"

DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(__file__), "nexterp_gen_addon_index_html.j2"
)
DEFAULT_PRESENTATION_MD = os.path.join(
    os.path.dirname(__file__), "nexterp_presentation.md"
)

# Fragments consumed by the branded template, in display order. The set
# is intentionally narrower than gen_addon_readme.FRAGMENTS — we only
# want what fits naturally into the tabbed layout.
HTML_FRAGMENTS = (
    "DESCRIPTION",
    "CONTEXT",
    "INSTALL",
    "CONFIGURE",
    "USAGE",
    "HISTORY",
)


def _make_md_renderer() -> MarkdownIt:
    return (
        MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True})
        .enable("table")
        .enable("strikethrough")
    )


def _rewrite_image_paths(md_text: str) -> str:
    """Strip ``./`` / ``../`` prefixes from relative image references.

    Screenshots live in ``static/description/`` next to the generated
    ``index.html``, so a fragment like ``![alt](./screenshot.png)`` (which
    resolves from ``readme/`` on GitHub) needs the prefix dropped here.
    Absolute URLs (http/https/data) and root-relative paths are left
    alone.
    """

    def _repl(match: "re.Match[str]") -> str:
        alt, path = match.group(1), match.group(2)
        if re.match(r"^(?:https?:|data:|/)", path):
            return match.group(0)
        cleaned = re.sub(r"^(?:\.{1,2}/)+", "", path)
        return f"![{alt}]({cleaned})"

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _repl, md_text)


def _split_fragment_intro(html: str) -> Dict[str, str]:
    """Pull the leading ``<h1>`` (if any) into a separate ``title`` slot."""
    m = re.match(
        r"\s*<h1[^>]*>(?P<title>.*?)</h1>\s*(?P<body>.*)",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if m:
        title = re.sub(r"<[^>]+>", "", m.group("title")).strip()
        return {"title": title, "body": m.group("body")}
    return {"title": "", "body": html}


def _render_fragments(addon_dir: str) -> Dict[str, Dict[str, str]]:
    """Render every ``readme/<NAME>.md`` fragment of ``addon_dir`` to HTML."""
    renderer = _make_md_renderer()
    out: Dict[str, Dict[str, str]] = {}
    for name in HTML_FRAGMENTS:
        path = os.path.join(addon_dir, "readme", name + ".md")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf8") as fh:
            md_text = fh.read()
        rendered = renderer.render(_rewrite_image_paths(md_text))
        out[name] = _split_fragment_intro(rendered)
    return out


def _find_icon_relpath(addon_dir: str) -> Optional[str]:
    for candidate in ("icon.png", "apps_icon.png"):
        if os.path.exists(os.path.join(addon_dir, "static", "description", candidate)):
            return candidate
    return None


def _find_sibling_icon_basename(sibling_dir: str) -> Optional[str]:
    for candidate in ("icon.png", "apps_icon.png"):
        if os.path.exists(os.path.join(sibling_dir, "static", "description", candidate)):
            return candidate
    return None


def _similarity_score(current_name: str, current_category: str,
                      sibling_name: str, sibling_category: str) -> int:
    """Rank a sibling vs the current addon for the "suite" carousel.

    Heuristic, no ML needed: longer shared snake_case prefix wins big
    (sibling addons with the same family prefix — ``nexterp_pos_*`` vs
    ``stock_manual_forecast_*`` — go to the top), a matching category
    breaks ties in the right direction, and unrelated addons fall to
    the bottom. Higher score is better.
    """
    cur_tokens = current_name.split("_")
    sib_tokens = sibling_name.split("_")
    common = 0
    for a, b in zip(cur_tokens, sib_tokens):
        if a != b:
            break
        common += 1
    score = common * 10
    if current_category and sibling_category == current_category:
        score += 5
    return score


def _build_siblings(
    addons: List,
    current_addon_name: str,
    current_category: str,
    branch: str,
    max_siblings: int,
) -> List[Dict[str, str]]:
    """Build the "other modules in the suite" list, capped at
    ``max_siblings`` items and ranked by similarity to the current addon
    (shared name prefix + shared category)."""
    candidates = []
    for s_name, s_dir, s_manifest in addons:
        if s_name == current_addon_name:
            continue
        if not s_manifest.get("installable", True):
            continue
        icon_basename = _find_sibling_icon_basename(s_dir)
        # Path is resolved relative to the *current* addon's
        # static/description/index.html, hence the ../../../ climb.
        icon_rel = (
            f"../../../{s_name}/static/description/{icon_basename}"
            if icon_basename
            else None
        )
        s_category = s_manifest.get("category", "")
        score = _similarity_score(
            current_addon_name, current_category, s_name, s_category
        )
        candidates.append(
            (
                score,
                {
                    "name": s_name,
                    "title": s_manifest.get("name", s_name),
                    "summary": (s_manifest.get("summary") or "").strip(),
                    "category": s_category,
                    "icon": icon_rel,
                    "apps_url": (
                        f"https://apps.odoo.com/apps/modules/{branch}/{s_name}"
                    ),
                },
            )
        )
    # Sort: highest score first, then by title for stable order across runs.
    candidates.sort(key=lambda c: (-c[0], c[1]["title"]))
    return [c[1] for c in candidates[:max_siblings]]


def _format_price(manifest: dict) -> str:
    price = manifest.get("price")
    if not price:
        return ""
    currency = manifest.get("currency", "EUR")
    symbol = {"EUR": "€", "USD": "$"}.get(currency, currency)
    try:
        amount = float(price)
    except (TypeError, ValueError):
        return f"{price} {currency}"
    if amount == int(amount):
        return f"{symbol}{int(amount)}"
    return f"{symbol}{amount:.2f}"


def _decide_tabs(fragments: Dict[str, Dict[str, str]]) -> List[str]:
    """Pick which tabs to render based on which fragments exist.

    Overview is always present. Features only appears when CONTEXT exists
    *in addition* to DESCRIPTION, otherwise CONTEXT is shown as Overview.
    """
    tabs: List[str] = []
    if "DESCRIPTION" in fragments or "CONTEXT" in fragments:
        tabs.append("overview")
    if "DESCRIPTION" in fragments and "CONTEXT" in fragments:
        tabs.append("features")
    if "CONFIGURE" in fragments or "INSTALL" in fragments:
        tabs.append("configure")
    if "USAGE" in fragments:
        tabs.append("usage")
    if "HISTORY" in fragments:
        tabs.append("versions")
    if not tabs:
        tabs.append("overview")
    return tabs


def _render_md_to_html(md_text: str) -> str:
    """Render a free-standing markdown blob (e.g. the shared NextERP intro)."""
    return _make_md_renderer().render(_rewrite_image_paths(md_text))


def gen_one_addon_index_html(
    addon_name: str,
    addon_dir: str,
    manifest: dict,
    branch: str,
    org_name: str,
    repo_name: str,
    html_template_filename: str,
    siblings: List[Dict[str, str]],
    nexterp_presentation_html: str = "",
) -> Optional[str]:
    """Render the branded ``index.html`` for one addon.

    Returns the path of the written file, or ``None`` if the existing
    ``index.html`` was hand-crafted (no generator marker) and preserved.
    """
    index_dir = os.path.join(addon_dir, "static", "description")
    index_filename = os.path.join(index_dir, "index.html")

    if os.path.exists(index_filename):
        with open(index_filename, "r", encoding="utf8") as fh:
            existing = fh.read()
        if OUR_MARKER not in existing and OCA_MARKER not in existing:
            return None

    os.makedirs(index_dir, exist_ok=True)

    fragments = _render_fragments(addon_dir)
    tab_ids = _decide_tabs(fragments)

    with open(html_template_filename, "r", encoding="utf8") as tf:
        template = Template(
            tf.read(), trim_blocks=True, lstrip_blocks=True, autoescape=False
        )

    html = template.render(
        marker=OUR_MARKER,
        addon_name=addon_name,
        manifest=manifest,
        fragments=fragments,
        tab_ids=tab_ids,
        siblings=siblings,
        branch=branch,
        org_name=org_name,
        repo_name=repo_name,
        icon=_find_icon_relpath(addon_dir),
        nexterp_presentation=nexterp_presentation_html,
        apps_url=f"https://apps.odoo.com/apps/modules/{branch}/{addon_name}",
    )

    with open(index_filename, "w", encoding="utf8") as fh:
        fh.write(html)
    return index_filename


@click.command()
@click.option(
    "--org-name", default="NextERP-Romania", help="GitHub organization name."
)
@click.option(
    "--repo-name", required=True, help="GitHub repository name, eg. odooapps."
)
@click.option(
    "--branch",
    required=True,
    help="Odoo series, eg. 19.0. Used in the Odoo Apps URLs of sibling cards.",
)
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
        "Directory containing several addons; index.html is generated for "
        "every installable addon found. Sibling cards are drawn from the "
        "same directory."
    ),
)
@click.option(
    "--template-filename",
    default=DEFAULT_TEMPLATE,
    help="Jinja2 HTML template. Defaults to the one shipped with the tool.",
)
@click.option(
    "--nexterp-presentation-md",
    default=DEFAULT_PRESENTATION_MD,
    help=(
        "Path to a markdown file rendered below the module's description "
        "in the Overview tab. Holds the shared NextERP company "
        "presentation. Pass an empty string to skip it entirely."
    ),
)
@click.option(
    "--max-siblings",
    type=int,
    default=8,
    help=(
        "Maximum number of sibling addons shown in the 'NextERP suite' "
        "section. Siblings are ranked by shared name prefix and category, "
        "so the cards closest to the current addon win the top slots."
    ),
)
def main(
    org_name,
    repo_name,
    branch,
    addon_dirs,
    addons_dir,
    template_filename,
    nexterp_presentation_md,
    max_siblings,
):
    """Generate NextERP-branded static/description/index.html files.

    Skips addons whose existing ``index.html`` has no generator marker —
    those are treated as hand-crafted and preserved.
    """
    presentation_html = ""
    if nexterp_presentation_md and os.path.exists(nexterp_presentation_md):
        with open(nexterp_presentation_md, "r", encoding="utf8") as fh:
            presentation_html = _render_md_to_html(fh.read())

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

    for addon_name, addon_dir, manifest in addons:
        if not manifest.get("installable", True):
            continue
        if not manifest.get("preloadable", True):
            continue
        # An addon with no readme/ fragments has nothing to render.
        if not any(
            os.path.exists(os.path.join(addon_dir, "readme", f + ".md"))
            for f in HTML_FRAGMENTS
        ):
            continue
        siblings = _build_siblings(
            addons,
            addon_name,
            manifest.get("category", ""),
            branch,
            max_siblings,
        )
        gen_one_addon_index_html(
            addon_name,
            addon_dir,
            manifest,
            branch,
            org_name,
            repo_name,
            template_filename,
            siblings,
            nexterp_presentation_html=presentation_html,
        )


if __name__ == "__main__":
    main()
