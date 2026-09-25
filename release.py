#!/usr/bin/env python3
"""release.py: build one public release of the iMessage texts plug-in, the same way every time.

Why this exists
---------------
The plug-in's working copy lives in its owner's Glitch folder at ``_local/imessage/``.
From its first run on, that folder also holds the owner's text messages: the stored
conversations (``threads/``), the queue and filing records (``ledger.*``, ``state.json``),
the summary pass's working files (``synth/``), card copies (``pre-backfill/``) and the
private settings with the owner's own numbers (``config.local.json``).
Publishing the plug-in means copying code out of a folder that also holds conversations.

Glitch's push-time scanner reads every push for keys: API keys, tokens, private keys.
It does not look for a conversation, a name, a phone number or an email address, so it
would let a transcript, or a test fixture built from a real text, straight through.
Two things in this file keep a member's texts out of a release instead:

1. An explicit ALLOWLIST. Nothing is copied by folder. Every shipped file is named, or
   matched by one narrow pattern (top-level ``*.py``, ``tests/*.py``). The private files
   are not on the list, so they cannot be picked up, and a symlink anywhere on the way is
   refused, never followed.
2. A SCAN of every staged file against a private denylist, ``.release-denylist`` at this
   workspace's root (the owner's own names, family, church, employers and numbers, one
   term per line, ``#`` comments; gitignored and never committed, because the list itself
   is private), plus built-in patterns for home-folder paths, email addresses and US
   phone numbers. Matching is case-insensitive and whole-word. Any hit stops the release.
   The report names ``file:line`` and the rule that fired, never the matched text, so the
   report cannot leak what it found.

The scan covers only the plug-in (what lands in ``src/imessage/``). The root files of this
repo (README, LICENSE, CHANGELOG) carry the author's name on purpose and are not scanned.

What a run does, in order
-------------------------
1. Reads the allowlisted files from the plug-in folder into memory.
2. Scans them. On any hit it stops here, so a failing release writes nothing at all:
   ``src/imessage/`` and ``dist/`` are left exactly as they were.
3. Wipes ``src/imessage/`` and writes the scanned bytes into it, then reads them back and
   checks every file's sha256 against what was scanned.
4. Builds ``dist/glitch-imessage-texts-vX.Y.Z.zip`` with one top folder ``imessage/``
   holding exactly those files, and prints the file count, zip size and zip sha256.

The zip is deterministic: entries sorted, fixed permissions, fixed compression, and every
entry dated with the release date from this version's ``CHANGELOG.md`` heading
(``## X.Y.Z (YYYY-MM-DD)``), so the same source and version always give the same bytes.
A version with no CHANGELOG entry is refused.

It never runs git and never touches the network. Standard library only; Python 3.9+.

Usage (from anywhere)::

    python3 release.py --version 0.1.0
    python3 release.py --version 0.1.0 --source /path/to/Glitch/_local/imessage
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The workspace sits at <Glitch>/workspaces/<this repo>; the plug-in at <Glitch>/_local/imessage.
DEFAULT_SOURCE = HERE.parents[1] / "_local" / "imessage"
STAGE_DIR = HERE / "src" / "imessage"
DIST_DIR = HERE / "dist"
DENYLIST = HERE / ".release-denylist"
GITIGNORE = HERE / ".gitignore"
CHANGELOG = HERE / "CHANGELOG.md"
ZIP_TOP = "imessage"
ZIP_NAME = "glitch-imessage-texts-v{version}.zip"

# --- the allowlist ---------------------------------------------------------------------
# Files shipped by name. Each one must exist.
NAMED_FILES = (
    "README.md",
    "capability.json",
    "config.json",
    "config.example.json",
    ".gitignore",
    "skill/SKILL.md",
)
# Folders whose direct *.py children ship ("" is the plug-in folder itself). Not recursive.
PY_GLOB_DIRS = ("", "tests")

# Private names that must never be staged, whatever the allowlist above says.
# A second wall: if a future edit to the allowlist ever reached one of these, the run stops.
PRIVATE_NAMES = frozenset(
    {
        "threads",
        "synth",
        "pre-backfill",
        "ledger.json",
        "ledger.wal",
        "ledger.lock",
        "state.json",
        "config.local.json",
        ".env",
        "__pycache__",
        ".release-denylist",
    }
)

# --- the built-in scan patterns ----------------------------------------------------------
# A letter or digit (Unicode-aware). Underscore and hyphen count as separators, so a term
# inside snake_case or kebab-case is still a whole word.
_ALNUM = r"[^\W_]"
_WORD_START = rf"(?<!{_ALNUM})"
_WORD_END = rf"(?!{_ALNUM})"

ALLOWED_EMAILS = frozenset({"noreply@anthropic.com"})
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org")  # RFC 2606 reserved, plus subdomains

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
    r"(?![A-Za-z0-9-])"
)
# A home folder with a concrete account name: /Users/<name>, \Users\<name>, /home/<name>.
HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[/\\](?:Users|home)[/\\]([A-Za-z0-9._-]+)", re.I)
HOME_PATH_PLACEHOLDERS = frozenset({"you", "yourname", "your-name", "your_name", "username", "example", "shared"})
# A US (NANP) number, 10 digits or 11 with a leading 1, bare or with ( ) . - or space.
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?([2-9]\d{2})[\s.-]?(\d{4})(?!\d)"
)


class ReleaseError(Exception):
    """A precondition failed; the message says what and what to do."""


# --- reading, never following a link ------------------------------------------------------


def _require_real_dir(path: Path, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ReleaseError(f"{label} not found: {path}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ReleaseError(f"{label} is a symlink; refusing to follow it: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise ReleaseError(f"{label} is not a folder: {path}")


def _read_regular_file(path: Path, rel: str) -> bytes:
    """Read one regular file without following a symlink, or refuse."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ReleaseError(f"allowlisted file missing: {rel}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ReleaseError(f"{rel} is a symlink; refusing to follow it")
    if not stat.S_ISREG(st.st_mode):
        raise ReleaseError(f"{rel} is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        fst = os.fstat(fh.fileno())
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise ReleaseError(f"{rel} changed while it was being read; run again")
        return fh.read()


def collect(source: Path) -> dict[str, bytes]:
    """Read exactly the allowlisted files from the plug-in folder: {relative path: bytes}."""
    _require_real_dir(source, "plug-in folder")
    for sub in {"tests", "skill"}:
        _require_real_dir(source / sub, f"plug-in folder '{sub}/'")

    rels: set[str] = set(NAMED_FILES)
    for sub in PY_GLOB_DIRS:
        folder = source / sub if sub else source
        with os.scandir(folder) as it:
            for entry in it:
                name = entry.name
                if not name.endswith(".py") or name.startswith("."):
                    continue
                rel = f"{sub}/{name}" if sub else name
                if entry.is_symlink():
                    raise ReleaseError(f"{rel} is a symlink; refusing to follow it")
                if not entry.is_file(follow_symlinks=False):
                    raise ReleaseError(f"{rel} matches *.py but is not a regular file")
                rels.add(rel)

    files: dict[str, bytes] = {}
    for rel in sorted(rels):
        parts = rel.split("/")
        if any(p in PRIVATE_NAMES for p in parts) or rel.endswith(".pyc"):
            raise ReleaseError(f"{rel} is private data and can never ship; check the allowlist")
        files[rel] = _read_regular_file(source.joinpath(*parts), rel)
    return files


# --- the scan -----------------------------------------------------------------------------


def load_denylist(path: Path) -> list[str]:
    """Terms, one per line; '#' starts a comment. A missing or empty list refuses the release."""
    if not path.is_file():
        raise ReleaseError(
            f"{path.name} not found at the workspace root. It lists the private names that must "
            "never ship (one per line) and is what keeps them out; refusing to release without it."
        )
    terms: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term and term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    if not terms:
        raise ReleaseError(f"{path.name} has no terms; refusing to release with an empty denylist.")
    return terms


def check_denylist_ignored(gitignore: Path) -> None:
    """The denylist is itself private: refuse unless .gitignore keeps it out of the repo."""
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.is_file() else []
    wanted = {".release-denylist", "/.release-denylist"}
    if not any(line.strip() in wanted for line in lines):
        raise ReleaseError(
            ".gitignore does not list .release-denylist. The denylist holds private names and "
            "must never be committed; add the line '.release-denylist' to .gitignore first."
        )


def _denylist_patterns(terms: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (f"denylist: {t}", re.compile(_WORD_START + re.escape(t) + _WORD_END, re.I)) for t in terms
    ]


def _email_ok(local: str, domain: str) -> bool:
    address = f"{local}@{domain}".lower()
    domain = domain.lower()
    if address in ALLOWED_EMAILS:
        return True
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def _builtin_hits(line: str) -> list[str]:
    rules: list[str] = []
    if any(not _email_ok(m.group(1), m.group(2)) for m in EMAIL_RE.finditer(line)):
        rules.append("built-in: email address")
    if any(m.group(1).lower() not in HOME_PATH_PLACEHOLDERS for m in HOME_PATH_RE.finditer(line)):
        rules.append("built-in: home-folder path")
    # The 555-0100 to 555-0199 range is reserved for fiction; the tests use it.
    if any(
        not (m.group(2) == "555" and m.group(3).startswith("01")) for m in PHONE_RE.finditer(line)
    ):
        rules.append("built-in: US phone number")
    return rules


def scan(files: dict[str, bytes], terms: list[str]) -> list[str]:
    """Every hit as 'imessage/<file>:<line>  <rule>'. Never includes the matched text."""
    deny = _denylist_patterns(terms)
    hits: list[str] = []
    for rel in sorted(files):
        shown = f"{ZIP_TOP}/{rel}"
        try:
            text = files[rel].decode("utf-8")
        except UnicodeDecodeError:
            hits.append(f"{shown}:0  not UTF-8 text, so it cannot be scanned")
            continue
        # Line 0 is the file's own path: a file named after a person is a leak too.
        lines = [rel] + text.split("\n")
        for number, line in enumerate(lines):
            rules = [label for label, rx in deny if rx.search(line)]
            rules += _builtin_hits(line)
            hits.extend(f"{shown}:{number}  {rule}" for rule in rules)
    return hits


# --- staging and the zip ------------------------------------------------------------------


def release_date(version: str) -> tuple[int, int, int, int, int, int]:
    """The date on this version's CHANGELOG heading, used for every zip entry."""
    text = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.is_file() else ""
    m = re.search(rf"^## {re.escape(version)} \((\d{{4}})-(\d{{2}})-(\d{{2}})\)", text, re.M)
    if not m:
        raise ReleaseError(
            f"CHANGELOG.md has no '## {version} (YYYY-MM-DD)' heading. Add the entry for this "
            "release first; its date stamps every file in the zip."
        )
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0)


def stage(files: dict[str, bytes]) -> None:
    """Wipe src/imessage/ and write exactly the scanned bytes, then prove it by reading back."""
    _require_real_dir(STAGE_DIR.parent, "src/")
    if os.path.lexists(STAGE_DIR):
        _require_real_dir(STAGE_DIR, "src/imessage/")
        shutil.rmtree(STAGE_DIR)
    for rel, data in sorted(files.items()):
        dest = STAGE_DIR.joinpath(*rel.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        os.chmod(dest, 0o644)
    for rel, data in files.items():
        back = _read_regular_file(STAGE_DIR.joinpath(*rel.split("/")), f"src/imessage/{rel}")
        if hashlib.sha256(back).digest() != hashlib.sha256(data).digest():
            raise ReleaseError(f"src/imessage/{rel} does not match what was scanned")


def build_zip(files: dict[str, bytes], out: Path, when: tuple[int, int, int, int, int, int]) -> None:
    """One top folder, sorted entries, fixed dates and permissions: same input, same bytes."""
    dirs = {ZIP_TOP + "/"}
    for rel in files:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add(ZIP_TOP + "/" + "/".join(parts[:i]) + "/")
    if os.path.lexists(DIST_DIR):
        _require_real_dir(DIST_DIR, "dist/")
    DIST_DIR.mkdir(exist_ok=True)
    tmp = out.with_name(out.name + ".partial")
    with zipfile.ZipFile(tmp, "w") as zf:
        for d in sorted(dirs):
            info = zipfile.ZipInfo(d, date_time=when)
            info.create_system = 3
            info.external_attr = (0o40755 << 16) | 0x10
            zf.writestr(info, b"")
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{ZIP_TOP}/{rel}", date_time=when)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[rel], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(tmp, out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Build one release of the iMessage texts plug-in.")
    parser.add_argument("--version", required=True, help="the release version, X.Y.Z")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"the plug-in folder to release from (default: {DEFAULT_SOURCE})",
    )
    args = parser.parse_args(argv)

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        print(f"release: --version must look like 1.2.3, got {args.version!r}", file=sys.stderr)
        return 2

    try:
        check_denylist_ignored(GITIGNORE)
        terms = load_denylist(DENYLIST)
        when = release_date(args.version)
        files = collect(args.source)
        hits = scan(files, terms)
        if hits:
            print(
                f"release: STOPPED. {len(hits)} hit(s) in the plug-in; nothing was written.\n"
                f"Paths are inside the plug-in folder ({args.source}).",
                file=sys.stderr,
            )
            for hit in hits:
                print(f"  {hit}", file=sys.stderr)
            return 1
        stage(files)
        out = DIST_DIR / ZIP_NAME.format(version=args.version)
        build_zip(files, out, when)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 2

    blob = out.read_bytes()
    print(f"release {args.version}")
    print(f"  scan:   {len(files)} files clean ({len(terms)} denylist terms + 3 built-in patterns)")
    print(f"  staged: {STAGE_DIR.relative_to(HERE)}/ ({len(files)} files)")
    print(f"  zip:    {out.relative_to(HERE)}")
    print(f"  files:  {len(files)}")
    print(f"  size:   {len(blob)} bytes")
    print(f"  sha256: {hashlib.sha256(blob).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
