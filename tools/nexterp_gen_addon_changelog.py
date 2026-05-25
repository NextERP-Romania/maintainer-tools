# License AGPLv3 (https://www.gnu.org/licenses/agpl-3.0-standalone.html)
# Copyright (c) 2026 NextERP Romania SRL
"""Auto-generate ``readme/HISTORY.md`` entries from git history on version bumps.

Designed as a pre-commit hook scoped to ``__manifest__\\.py$``. When the
working-tree manifest version differs from the version committed at
``HEAD`` for an addon, the hook concludes that the developer is bumping
the version and writes (or refreshes) the changelog entry that
corresponds to the *previous* version line — i.e. the body of commits
that touched that addon between the moment the old version was
introduced and ``HEAD``. The new bump itself is left to be recorded on
the next bump, because the bump commit does not yet exist at pre-commit
time.

The entry is appended to (or replaces, when already present) the
``readme/HISTORY.md`` file in OCA-compatible markdown:

    # Changelog

    ## <version> (<YYYY-MM-DD>)

    - [<short-sha>] <commit subject>
    - ...

The pre-commit framework then notices ``HISTORY.md`` was touched and
asks the developer to re-stage it. The standard ``oca-gen-addon-readme``
hook downstream picks up the new fragment for ``README.rst``, and
``nexterp-gen-addon-index-html`` shows it in the Versions tab.

Idempotency: re-running the script when there is no bump is a no-op.
Re-running while a bump is still pending refreshes the entry for the
*current* (working-tree) version with the latest commit list.
"""
import datetime
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import click

from .manifest import NoManifestFound, find_addons, get_manifest_path, read_manifest

HISTORY_FILENAME = "HISTORY.md"
HISTORY_TITLE = "# Changelog\n"
ENTRY_HEADER_RE = re.compile(
    r"^## (?P<version>[^\s]+)(?:\s+\((?P<date>[^)]+)\))?\s*$",
    re.MULTILINE,
)


def _run_git(args: List[str], cwd: str) -> str:
    """Run git and return stripped stdout. Raises CalledProcessError on failure."""
    return subprocess.check_output(
        ["git"] + args, cwd=cwd, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _repo_root(start: str) -> Optional[str]:
    try:
        return _run_git(["rev-parse", "--show-toplevel"], start)
    except subprocess.CalledProcessError:
        return None


def _manifest_version_at(repo_root: str, manifest_relpath: str, ref: str) -> Optional[str]:
    """Parse the manifest at the given git ref and return its ``version`` field."""
    try:
        content = subprocess.check_output(
            ["git", "show", f"{ref}:{manifest_relpath}"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
        ).decode("utf8")
    except subprocess.CalledProcessError:
        return None
    return _parse_version_from_manifest_content(content)


def _parse_version_from_manifest_content(content: str) -> Optional[str]:
    """Pull the ``version`` value out of a manifest source string.

    Uses a regex because ``ast.literal_eval`` chokes on the typical Python
    manifest (it uses ``True`` etc., not pure literals), and we only need
    a single string field.
    """
    m = re.search(
        r"""['"]version['"]\s*:\s*['"]([^'"]+)['"]""",
        content,
    )
    return m.group(1) if m else None


def _find_old_version_first_sha(
    repo_root: str, manifest_relpath: str, old_version: str
) -> Optional[str]:
    """Walk ``git log`` of the manifest newest-first. Return the oldest SHA
    whose manifest version equals ``old_version`` — i.e. the bump commit
    where ``old_version`` was introduced. Returns ``None`` if not found
    (e.g. the addon's first version: no previous bump in history).
    """
    try:
        shas = _run_git(
            ["log", "--format=%H", "--", manifest_relpath], repo_root
        ).splitlines()
    except subprocess.CalledProcessError:
        return None
    oldest = None
    for sha in shas:
        v = _manifest_version_at(repo_root, manifest_relpath, sha)
        if v == old_version:
            oldest = sha
        elif oldest is not None:
            # We've passed the boundary: the previous SHA we kept (oldest)
            # is the bump that introduced old_version.
            break
    return oldest


def _commits_in_range(
    repo_root: str, addon_relpath: str, since_sha: Optional[str]
) -> List[Tuple[str, str]]:
    """Return ``[(short_sha, subject), …]`` for commits touching the addon dir.

    When ``since_sha`` is given, the range is the parent of that SHA
    (exclusive) to ``HEAD`` (inclusive), so the bump that introduced
    ``old_version`` is itself counted as the *first* change of that line.
    When ``since_sha`` is None (no prior version in history), all
    commits touching the addon dir are returned.
    """
    if since_sha:
        # `<sha>^..HEAD` may fail if <sha> is the root commit; fall back to
        # listing everything reachable from HEAD in that case.
        rev_range = f"{since_sha}^..HEAD"
        try:
            _run_git(["rev-parse", "--verify", f"{since_sha}^"], repo_root)
        except subprocess.CalledProcessError:
            rev_range = "HEAD"
    else:
        rev_range = "HEAD"
    try:
        raw = _run_git(
            [
                "log",
                rev_range,
                "--format=%h\x1f%s",
                "--no-merges",
                "--",
                addon_relpath,
            ],
            repo_root,
        )
    except subprocess.CalledProcessError:
        return []
    out = []
    for line in raw.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        out.append((sha.strip(), subject.strip()))
    return out


def _strip_oca_commit_tags(subject: str) -> str:
    """Normalize subjects like ``[IMP] addon: do foo`` → ``IMP: do foo``."""
    m = re.match(
        r"^\[(?P<tag>[A-Z]+)\]\s*(?:[a-z0-9_]+:\s*)?(?P<rest>.*)$",
        subject,
    )
    if m:
        return f"{m.group('tag')}: {m.group('rest').strip()}"
    return subject


def _format_entry(
    version: str, today: datetime.date, commits: List[Tuple[str, str]]
) -> str:
    lines = [f"## {version} ({today.isoformat()})", ""]
    if commits:
        for sha, subject in commits:
            lines.append(f"- [{sha}] {_strip_oca_commit_tags(subject)}")
    else:
        # Baseline / first-run stamp — left intentionally empty so it
        # reads as "this is when tracking started for this module".
        lines.append("- _Changelog tracking starts at this release._")
    lines.append("")
    return "\n".join(lines)


def _read_history(history_path: str) -> str:
    if not os.path.exists(history_path):
        return ""
    with open(history_path, "r", encoding="utf8") as fh:
        return fh.read()


def _write_history(history_path: str, content: str) -> None:
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "w", encoding="utf8") as fh:
        fh.write(content)


def _upsert_entry(existing: str, version: str, entry: str) -> str:
    """Return the new HISTORY.md content with ``entry`` representing
    ``version`` either inserted at the top (newest-first) or replacing
    any pre-existing entry for the same version."""
    body = existing
    if not body.lstrip().startswith("# Changelog"):
        body = HISTORY_TITLE + "\n" + body.lstrip()

    # Find an existing entry for this version and remove it.
    new_body_lines = []
    skipping = False
    for line in body.splitlines(keepends=True):
        header = ENTRY_HEADER_RE.match(line.rstrip("\n"))
        if header:
            skipping = header.group("version") == version
            if skipping:
                continue
        elif skipping:
            if line.startswith("## "):
                skipping = False
            else:
                continue
        if not skipping:
            new_body_lines.append(line)
    body = "".join(new_body_lines).rstrip() + "\n"

    # Insert the new entry right after the `# Changelog` header.
    head, _, rest = body.partition("\n")
    if head.strip() == "# Changelog":
        return f"{head}\n\n{entry.rstrip()}\n\n{rest.lstrip()}".rstrip() + "\n"
    return f"{HISTORY_TITLE}\n{entry.rstrip()}\n\n{body.lstrip()}".rstrip() + "\n"


def update_one_addon(addon_dir: str) -> Optional[str]:
    """Update ``readme/HISTORY.md`` for one addon when a version bump is
    detected. Returns the new file path on change, ``None`` otherwise.

    First-run behavior: if ``HISTORY.md`` does not yet exist, the tool
    writes a baseline entry for the *current* version with no commit
    list. This is the "track from now on" mode — historical commits are
    intentionally not backfilled. Subsequent bumps then list only the
    commits that landed between that baseline and the new bump.
    """
    try:
        manifest_on_disk = read_manifest(addon_dir)
    except NoManifestFound:
        return None
    new_version = manifest_on_disk.get("version")
    if not new_version:
        return None

    manifest_path = get_manifest_path(addon_dir)
    repo_root = _repo_root(addon_dir)
    if not repo_root:
        return None  # not in a git repo
    manifest_relpath = os.path.relpath(manifest_path, repo_root)
    addon_relpath = os.path.relpath(addon_dir, repo_root)

    head_version = _manifest_version_at(repo_root, manifest_relpath, "HEAD")
    history_path = os.path.join(addon_dir, "readme", HISTORY_FILENAME)
    history_existed = os.path.exists(history_path)

    # Nothing to do on a no-op run: same version AND we already have a
    # changelog file (so the baseline is already in place).
    if (
        head_version is not None
        and head_version == new_version
        and history_existed
    ):
        return None

    if not history_existed:
        # First run for this addon — stamp the current version as the
        # baseline and stop. Future bumps will track commits from here.
        commits: List[Tuple[str, str]] = []
    else:
        old_version = head_version
        boundary_sha = None
        if old_version:
            boundary_sha = _find_old_version_first_sha(
                repo_root, manifest_relpath, old_version
            )
        commits = _commits_in_range(repo_root, addon_relpath, boundary_sha)

    entry = _format_entry(new_version, datetime.date.today(), commits)
    existing = _read_history(history_path)
    new_content = _upsert_entry(existing, new_version, entry)
    if new_content == existing:
        return None
    _write_history(history_path, new_content)
    return history_path


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
        "Directory containing several addons. The hook walks every "
        "installable addon and updates HISTORY.md for those whose "
        "manifest version differs from the version at HEAD."
    ),
)
def main(addon_dirs, addons_dir):
    """Refresh ``readme/HISTORY.md`` from git when ``version`` changes.

    Iterates each addon, parses the manifest version in the working
    tree, compares it with the manifest at ``HEAD``, and on a mismatch
    writes a markdown entry listing the commits that touched the addon
    during the previous version's lifespan.
    """
    seen = set()
    addons = []

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
        addon_name = Path(addon_dir).resolve().name
        try:
            manifest = read_manifest(addon_dir)
        except NoManifestFound:
            continue
        _add(addon_name, addon_dir, manifest)

    for _name, addon_dir, manifest in addons:
        if not manifest.get("installable", True):
            continue
        update_one_addon(addon_dir)


if __name__ == "__main__":
    main()
