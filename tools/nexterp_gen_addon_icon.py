# License AGPLv3 (https://www.gnu.org/licenses/agpl-3.0-standalone.html)
# Copyright (c) 2026 NextERP Romania SRL
"""Auto-generate a NextERP-branded ``icon.png`` / ``apps_icon.png`` per addon.

When an addon ships in ``odooapps`` without its own icon, this hook
renders a deterministic SVG from a Jinja template — colored according
to the manifest category, stamped with a 2-3 letter initial drawn from
the addon name, and badged with a category-specific emoji in the
corner. The SVG is then rasterized to PNG via ImageMagick (``convert``;
``rsvg-convert`` or ``inkscape`` are tried as fallbacks) at 256×256 for
``icon.png`` and 1058×595 for ``apps_icon.png``.

The hook never overwrites an existing icon file: ship a hand-crafted
``icon.png``/``icon.svg``/``apps_icon.png`` and this tool stays out of
the way.
"""
import os
import re
import shutil
import subprocess
from typing import Dict, Optional, Tuple

import click
from jinja2 import Template

from .manifest import NoManifestFound, find_addons, read_manifest

DEFAULT_ICON_TPL = os.path.join(
    os.path.dirname(__file__), "nexterp_addon_icon.svg.j2"
)
DEFAULT_APPS_ICON_TPL = os.path.join(
    os.path.dirname(__file__), "nexterp_apps_icon.svg.j2"
)

# Category → (gradient light, gradient dark, emoji).
# Keys are matched substring-style against the manifest category, so
# "Localization/Romania" still hits the Romania entry.
CATEGORY_THEMES: Tuple[Tuple[str, str, str, str], ...] = (
    ("Romania",                 "#ef4444", "#b91c1c", "🇷🇴"),
    ("Localization",            "#dc2626", "#7f1d1d", "🌍"),
    ("Point of Sale",           "#14b8a6", "#0f766e", "🏪"),
    ("Inventory",               "#3b82f6", "#1d4ed8", "📦"),
    ("Warehouse",               "#3b82f6", "#1d4ed8", "📦"),
    ("Stock",                   "#3b82f6", "#1d4ed8", "📦"),
    ("Manufacturing",           "#6366f1", "#4338ca", "🛠️"),
    ("Purchase",                "#0891b2", "#155e75", "🛒"),
    ("Sales",                   "#f97316", "#c2410c", "💰"),
    ("Subscription",            "#f59e0b", "#b45309", "🔁"),
    ("Accounting",              "#8b5cf6", "#6d28d9", "🧾"),
    ("Productivity/Documents",  "#475569", "#1e293b", "📄"),
    ("Productivity",            "#10b981", "#047857", "✅"),
    ("Human Resources",         "#ec4899", "#be185d", "👥"),
    ("Website",                 "#06b6d4", "#0e7490", "🌐"),
)
DEFAULT_THEME = ("#e11d48", "#9f1239", "⚙️")


def _pick_theme(category: Optional[str]) -> Tuple[str, str, str]:
    """Return ``(color_light, color_dark, emoji)`` for the given category."""
    if not category:
        return DEFAULT_THEME
    for needle, light, dark, emoji in CATEGORY_THEMES:
        if needle.lower() in category.lower():
            return (light, dark, emoji)
    return DEFAULT_THEME


# Trim these snake_case prefixes/suffixes before computing initials, so
# ``nexterp_sgr_account`` reads as ``SGR account`` not ``NSA``.
STRIP_PREFIXES = ("nexterp_", "l10n_ro_", "l10n_")


def _compute_initials(addon_name: str) -> Tuple[str, int]:
    """Pick initials + a font size matched to their length.

    For 1-token names we take the first 3 characters of the token
    (``sgr → SGR``). For multi-token names we take the first letter of
    each token, capped at 4 (``sgr_account → SA`` after prefix-strip).
    The font size shrinks as the initials get wider so a 4-letter
    monogram still fits in the same medallion.
    """
    name = addon_name
    for p in STRIP_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    tokens = [t for t in name.split("_") if t]
    if len(tokens) == 1:
        initials = tokens[0][:3].upper()
    else:
        initials = "".join(t[0] for t in tokens[:4]).upper()
    n = len(initials)
    if n <= 2:
        size = 160
    elif n == 3:
        size = 128
    else:
        size = 100
    return initials, size


def _split_title(title: str, max_per_line: int = 18) -> Tuple[str, str]:
    """Soft-wrap the manifest ``name`` onto at most two lines for the banner.

    The 18-char ceiling matches the empirical width budget of the title
    column at 56-64px font weight 800; anything wider clips against the
    right edge of the 1058-wide canvas. Long titles past two lines get
    truncated on the second line with an ellipsis so the layout stays
    rectangular.
    """
    words = title.split()
    line1, line2 = "", ""
    for w in words:
        if not line1 or len((line1 + " " + w).strip()) <= max_per_line:
            line1 = (line1 + " " + w).strip()
            continue
        candidate2 = (line2 + " " + w).strip()
        if len(candidate2) <= max_per_line:
            line2 = candidate2
        else:
            # Second line full — append … and stop. We'd rather lose
            # a few words than have the title escape the banner.
            line2 = (line2 + "…").strip()
            break
    return line1, line2


def _render_svg_to_png(svg_text: str, png_path: str, width: int, height: int) -> bool:
    """Rasterize an in-memory SVG to PNG at the requested size.

    Prefers ``cairosvg`` (pure-Python Cairo binding — clean gradients,
    real font rendering, color emoji via Pango when available). Falls
    back to ``rsvg-convert``, then ImageMagick ``convert``. We avoid
    ImageMagick when we can: its SVG support drops gradients and
    flattens text, which produced unusable icons during testing.
    """
    # 1) cairosvg path — handles SVG natively in Python.
    try:
        import cairosvg
    except ImportError:
        cairosvg = None
    if cairosvg is not None:
        try:
            cairosvg.svg2png(
                bytestring=svg_text.encode("utf8"),
                write_to=png_path,
                output_width=width,
                output_height=height,
            )
            return True
        except Exception:
            pass

    # 2) CLI fallbacks. Persist the SVG to a temp file first.
    cli = None
    if shutil.which("rsvg-convert"):
        cli = "rsvg-convert"
    elif shutil.which("convert"):
        cli = "convert"
    elif shutil.which("inkscape"):
        cli = "inkscape"
    if cli is None:
        return False

    tmp_svg = png_path + ".tmp.svg"
    with open(tmp_svg, "w", encoding="utf8") as fh:
        fh.write(svg_text)
    try:
        if cli == "rsvg-convert":
            cmd = [
                "rsvg-convert",
                "--width", str(width),
                "--height", str(height),
                "--output", png_path,
                tmp_svg,
            ]
        elif cli == "convert":
            cmd = [
                "convert",
                "-background", "none",
                "-density", "300",
                "-resize", f"{width}x{height}",
                tmp_svg,
                png_path,
            ]
        else:  # inkscape
            cmd = [
                "inkscape",
                tmp_svg,
                "--export-type=png",
                f"--export-filename={png_path}",
                f"--export-width={width}",
                f"--export-height={height}",
            ]
        subprocess.check_call(cmd, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False
    finally:
        if os.path.exists(tmp_svg):
            os.remove(tmp_svg)


def _render(template_path: str, **kwargs: object) -> str:
    with open(template_path, "r", encoding="utf8") as fh:
        return Template(
            fh.read(), trim_blocks=True, lstrip_blocks=True, autoescape=False
        ).render(**kwargs)


def gen_one_addon_icon(
    addon_name: str,
    addon_dir: str,
    manifest: dict,
    icon_template: str,
    apps_icon_template: str,
) -> Dict[str, str]:
    """Generate missing icons for one addon. Returns ``{kind: path}``
    for every file actually written this run (so the caller can hand
    them to pre-commit's "files changed" reporting)."""
    written = {}
    description_dir = os.path.join(addon_dir, "static", "description")
    os.makedirs(description_dir, exist_ok=True)

    category = manifest.get("category", "") or ""
    color_light, color_dark, emoji = _pick_theme(category)
    initials, initials_size = _compute_initials(addon_name)
    title = manifest.get("name") or addon_name
    line1, line2 = _split_title(title)

    icon_png = os.path.join(description_dir, "icon.png")
    icon_svg = os.path.join(description_dir, "icon.svg")
    if not os.path.exists(icon_png) and not os.path.exists(icon_svg):
        svg = _render(
            icon_template,
            color_light=color_light,
            color_dark=color_dark,
            emoji=emoji,
            initials=initials,
            initials_font_size=initials_size,
            width=256,
            height=256,
        )
        with open(icon_svg, "w", encoding="utf8") as fh:
            fh.write(svg)
        written["icon.svg"] = icon_svg
        if _render_svg_to_png(svg, icon_png, 256, 256):
            written["icon.png"] = icon_png

    apps_icon_png = os.path.join(description_dir, "apps_icon.png")
    if not os.path.exists(apps_icon_png):
        svg = _render(
            apps_icon_template,
            color_light=color_light,
            color_dark=color_dark,
            emoji=emoji,
            initials=initials,
            initials_font_size_banner=max(initials_size + 40, 140),
            name_line_1=line1,
            name_line_2=line2,
            category=category,
            title_font_size=64 if not line2 else 56,
            width=1058,
            height=595,
        )
        if _render_svg_to_png(svg, apps_icon_png, 1058, 595):
            written["apps_icon.png"] = apps_icon_png

    return written


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
        "Directory containing several addons; every installable addon "
        "missing an icon.png/apps_icon.png gets one generated."
    ),
)
@click.option(
    "--icon-template",
    default=DEFAULT_ICON_TPL,
    help="Jinja2 SVG template for the 256x256 icon.",
)
@click.option(
    "--apps-icon-template",
    default=DEFAULT_APPS_ICON_TPL,
    help="Jinja2 SVG template for the 1058x595 banner.",
)
def main(addon_dirs, addons_dir, icon_template, apps_icon_template):
    """Generate missing icon.png and apps_icon.png for each addon."""
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
        gen_one_addon_icon(
            addon_name, addon_dir, manifest, icon_template, apps_icon_template
        )


if __name__ == "__main__":
    main()
