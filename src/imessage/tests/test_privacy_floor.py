"""The privacy floor — the gate on the whole iMessage plug-in build.

This is the one suite that must stay green at every later checkpoint, because
everything it guards is a member's own text messages.  It proves four things:
the plug-in's `.gitignore` names every private path as a real ignore rule (not
in a comment); a real decoy file written at each of those paths is ACTIVELY
ignored by this repo's git — `check-ignore` confirming the rule, not merely
absence from `status`; the LIVE `/local-backup` enrolment record still carries
all five kept-local exclusions AND really withholds a file sitting under
`threads/`; and the Brain's own repo never sees this plug-in at all.  A red here
means something private is one `push` away from travelling, so the build stops.

Where a check's precondition is absent on a machine, it SKIPS with the reason
rather than failing: no git work tree around this folder (git has nothing it
could commit), or a `/local-backup` that has not enrolled this plug-in (nothing
of it is backed up).  Where the precondition is present and the private paths
are not protected, it is red, exactly as before.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's OWN config comes first, before anything reachable only because
# it put `.claude/scripts` on sys.path.  This suite imports no engine module at
# all, so the ordering can never come down to test-discovery luck.
import imconfig  # noqa: E402, I001 - FIRST on purpose (see above), never sorted after stdlib

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

BRAIN_ROOT = PLUGIN_HOME.parents[1]
IGNORE_FILE = PLUGIN_HOME / ".gitignore"

# Every rule the ignore file must carry, exactly as git must read it.  `synth/`
# holds the summary claim files (each lists a day's absolute transcript paths),
# `pre-backfill/` the copies of person cards taken before a backfill, and
# `ledger.lock` the ledger's lock file: all three are private run state.
REQUIRED_IGNORE_RULES = (
    "threads/",
    "synth/",
    "pre-backfill/",
    "state.json",
    "ledger.json",
    "ledger.wal",
    "ledger.lock",
    "config.local.json",
    ".env",
    "__pycache__/",
    "*.pyc",
)

# The five paths `/local-backup` was told to keep on this machine, spelled the
# way the enrolment records them (item-relative, no trailing slash).
REQUIRED_KEPT_LOCAL = (
    "threads",
    "state.json",
    "ledger.json",
    "ledger.wal",
    "config.local.json",
)

# A decoy per private path: (path relative to the plug-in home, the .gitignore
# rule git must cite when it refuses it).  The threads decoy carries a space in
# its name because that is how a real per-thread file is named.
#: The Brain's own blanket ignore for every member plug-in (in the Brain's `.gitignore`).
#: With no repo in this folder, `git -C` answers from the Brain and THIS is the
#: rule doing the protecting. See TestGitCannotSeeAnyPrivateFile's docstring.
BRAIN_LOCAL_RULE = "/_local/"

THREADS_DECOY = "threads/2020-01-01 decoy 1.txt"
DECOYS = (
    (THREADS_DECOY, "threads/"),
    ("state.json", "state.json"),
    ("ledger.json", "ledger.json"),
    ("ledger.wal", "ledger.wal"),
    ("config.local.json", "config.local.json"),
    (".env", ".env"),
)

DECOY_BODY = "privacy-floor decoy — not real data; delete freely.\n"

# What imconfig must aim each private path at, for the ignore rules above to
# cover the files this plug-in actually writes.
PRIVATE_PATH_ATTRS = {
    "THREADS_DIR": "threads",
    "STATE_PATH": "state.json",
    "LEDGER_PATH": "ledger.json",
    "LEDGER_WAL": "ledger.wal",
    "LOCAL_CONFIG_PATH": "config.local.json",
}


def _ignore_rules():
    """Every real rule in the plug-in's .gitignore — comments and blanks dropped."""
    rules = []
    for raw in IGNORE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(line)
    return rules


def _git(*args, cwd=None):
    """Run a READ-ONLY git command and hand back the completed process."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _inside_a_work_tree(folder):
    """True when git sees ``folder`` as inside a work tree, so it could commit from it."""
    proc = _git("-C", str(folder), "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _status_paths(porcelain):
    """Every path named by `git status --porcelain` output, unquoted."""
    paths = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        for part in rest.split(" -> "):
            part = part.strip()
            if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
                part = part[1:-1]
            if part:
                paths.append(part)
    return paths


def _plant(target):
    """Create ``target`` holding the decoy, or return ``None`` if ANYTHING is there.

    Exclusive create (``open(path, "x")``, ``O_CREAT | O_EXCL`` underneath): the check
    and the create are one step in the kernel, so a real run that writes ``state.json``
    or the ledger in the gap can never be overwritten by a decoy.  An ``exists()`` check
    followed by a write could: the file would appear between the two, and the decoy
    would replace it.  Returns the new file's ``(st_dev, st_ino)``, its identity, which
    :func:`_remove_if_ours` checks before it deletes anything.
    """
    try:
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(DECOY_BODY)
            stat_result = os.fstat(handle.fileno())
    except FileExistsError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)


def _remove_if_ours(target, identity):
    """Delete ``target`` only while it is still the very file this suite created.

    A real run replaces ``state.json`` and ``ledger.json`` by rename (a new inode) and
    appends to ``ledger.wal`` in place (new bytes), so a file whose identity or contents
    changed since it was planted is real data now, and it is left exactly where it is.
    """
    try:
        stat_result = os.stat(target)
        same = (stat_result.st_dev, stat_result.st_ino) == identity
        if not same or target.read_text(encoding="utf-8") != DECOY_BODY:
            return False
        target.unlink()
        return True
    except (FileNotFoundError, UnicodeDecodeError):
        return False


class DecoyMixin:
    """Writes one decoy per private path, and removes every one of them again.

    A path that is already on the disk is left completely alone: it is real data, it
    is still asserted on, and this suite never deletes it.  Decoys are planted with an
    exclusive create and removed only while still byte-for-byte the file this suite
    made, so a real run racing the suite can never have its state overwritten, and
    never have it deleted.  ``decoy_home`` exists so the mixin can be proved on a
    temporary folder; everywhere else it is the plug-in home.
    """

    decoy_home = PLUGIN_HOME

    def _plant_decoys(self):
        self._planted = []
        self._made_threads_dir = False
        threads_dir = self.decoy_home / "threads"
        try:
            threads_dir.mkdir(parents=True)
            self._made_threads_dir = True
        except FileExistsError:
            pass
        for rel, _rule in DECOYS:
            target = self.decoy_home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            identity = _plant(target)
            if identity is not None:
                self._planted.append((target, identity))

    def _clear_decoys(self):
        for target, identity in getattr(self, "_planted", []):
            _remove_if_ours(target, identity)
        self._planted = []
        threads_dir = self.decoy_home / "threads"
        if getattr(self, "_made_threads_dir", False) and threads_dir.is_dir():
            with contextlib.suppress(OSError):
                threads_dir.rmdir()  # only ever an EMPTY folder: rmdir refuses anything else
        self._made_threads_dir = False


class TestIgnoreFileNamesEveryPrivatePath(unittest.TestCase):
    """Test 1 — the ignore file names every private path, as a rule, not a comment."""

    def test_every_required_rule_is_a_real_ignore_line(self):
        self.assertTrue(
            IGNORE_FILE.is_file(),
            f"PRIVACY FLOOR: {IGNORE_FILE} does not exist, so NOTHING in this plug-in "
            "is ignored — every thread, the state file and the ledger would travel.",
        )
        rules = _ignore_rules()
        for rule in REQUIRED_IGNORE_RULES:
            self.assertIn(
                rule,
                rules,
                f"PRIVACY FLOOR: '{rule}' is not an ignore rule in {IGNORE_FILE} "
                f"(rules actually present: {rules}). Anything at that path would be "
                "committed and pushed.",
            )

    def test_imconfig_aims_every_private_path_inside_the_ignored_home(self):
        """A private path aimed outside the plug-in home is covered by no rule at all."""
        home = Path(str(getattr(imconfig, "HOME", ""))).resolve()
        self.assertEqual(
            home,
            PLUGIN_HOME,
            f"PRIVACY FLOOR: imconfig.HOME is {home}, not the plug-in home {PLUGIN_HOME}; "
            "the .gitignore protecting this build would not cover what the plug-in writes.",
        )
        brain = Path(str(getattr(imconfig, "BRAIN_ROOT", ""))).resolve()
        self.assertEqual(
            brain,
            BRAIN_ROOT,
            f"PRIVACY FLOOR: imconfig.BRAIN_ROOT is {brain}, not {BRAIN_ROOT}; "
            "the plug-in is pointed at the wrong repo.",
        )
        for attr, expected_name in PRIVATE_PATH_ATTRS.items():
            value = getattr(imconfig, attr, None)
            self.assertIsNotNone(
                value,
                f"PRIVACY FLOOR: imconfig has no {attr}, so nothing pins the private "
                f"'{expected_name}' path that the ignore rules protect.",
            )
            target = Path(str(value)).resolve()
            self.assertEqual(
                target.name,
                expected_name,
                f"PRIVACY FLOOR: imconfig.{attr} points at '{target.name}', but the "
                f"ignore rule and the kept-local enrolment both name '{expected_name}'. "
                "The file the plug-in writes would not be the file that is ignored.",
            )
            self.assertIn(
                PLUGIN_HOME,
                target.parents,
                f"PRIVACY FLOOR: imconfig.{attr} is {target}, which is OUTSIDE the "
                f"plug-in home {PLUGIN_HOME}; no rule in this repo's .gitignore reaches it.",
            )


class TestGitCannotSeeAnyPrivateFile(DecoyMixin, unittest.TestCase):
    """Test 2 — a real file at each private path is invisible to git.

    This plug-in deliberately has NO repo of its own.  One was tried and removed
    on 2026-09-20: ``guard_rules.is_brain_history_write`` refuses every commit for
    any directory inside the Brain root that is not under ``workspaces/``, and the
    test is purely lexical on the path, so a nested ``.git`` is invisible to it —
    ``git init`` succeeds and every commit is denied.  ``local_backup`` would have
    dropped the history anyway.

    So the rule that actually protects these files is the BRAIN's ``/_local/``
    (in the Brain's own ``.gitignore``), and ``git -C`` from this folder answers from the Brain.
    These tests assert that truth rather than the old one.  The plug-in's own
    ``.gitignore`` is kept as intent — it is what would protect the files the day
    this folder becomes a repo, and Test 1 still holds it to naming every private
    path — but it is not load-bearing today and this suite must not pretend it is.
    """

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not on PATH, so the ignore rules cannot be checked here")
        if not _inside_a_work_tree(PLUGIN_HOME):
            self.skipTest(
                "this plug-in folder is not inside a git work tree, so git has nothing it "
                "could commit from it; Test 1 still holds the .gitignore to every private path"
            )
        self._plant_decoys()

    def tearDown(self):
        self._clear_decoys()

    def test_no_private_decoy_appears_in_git_status(self):
        # Scoped to this folder (`-- .`) so nothing else in the Brain can match a
        # decoy's name, and every untracked file listed one by one (`-uall`) so a
        # visible decoy can never hide inside a collapsed folder line.
        proc = _git(
            "-C", str(PLUGIN_HOME), "status", "--porcelain", "--untracked-files=all", "--", "."
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"PRIVACY FLOOR: `git status` failed in {PLUGIN_HOME}: {proc.stderr.strip()}",
        )
        out = proc.stdout
        for rel, _rule in DECOYS:
            needle = Path(rel).name
            self.assertNotIn(
                needle,
                out,
                f"PRIVACY FLOOR: git can SEE '{rel}' — it is listed by `git status` "
                f"in {PLUGIN_HOME}. Private data would be committed.\n"
                f"status was:\n{out}",
            )

    def test_git_actively_confirms_each_private_decoy_is_ignored(self):
        for rel, rule in DECOYS:
            with self.subTest(path=rel):
                target = PLUGIN_HOME / rel
                self.assertTrue(
                    target.exists(),
                    f"PRIVACY FLOOR: decoy '{rel}' was not written, so nothing was proven.",
                )
                proc = _git("-C", str(PLUGIN_HOME), "check-ignore", "-v", "--", rel)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"PRIVACY FLOOR: git does NOT ignore '{rel}' "
                    f"(check-ignore exit {proc.returncode}). That file would travel.\n"
                    f"stdout: {proc.stdout.strip()!r} stderr: {proc.stderr.strip()!r}",
                )
                source_field = proc.stdout.split("\t")[0]
                cited_rule = source_field.rsplit(":", 1)[-1]
                # Either protection is acceptable; NEITHER is the failure. Today the
                # Brain's blanket `/_local/` does the work (no repo here); if this
                # folder ever becomes a repo, its own rule takes over. Both are named
                # so a silent change of guard is visible in the failure text.
                self.assertIn(
                    cited_rule,
                    (rule, BRAIN_LOCAL_RULE),
                    f"PRIVACY FLOOR: '{rel}' is ignored by rule '{cited_rule}' from "
                    f"'{source_field}' — neither this plug-in's own '{rule}' nor the "
                    f"Brain's '{BRAIN_LOCAL_RULE}'. Something unrelated is the only "
                    "thing standing between this file and a commit, and it could stop "
                    "at any time.",
                )


class TestLiveKeptLocalEnrolment(DecoyMixin, unittest.TestCase):
    """Test 3 — the LIVE /local-backup record really withholds the five private paths.

    This reads the real enrolment through `local_backup.py discover`. It never
    fabricates entries.  A plug-in `/local-backup` has not enrolled travels
    nowhere, so on such a machine these tests skip with that reason.  Once it IS
    enrolled, a kept-local record missing any of the five paths has to show up red
    here, because the day it does not is the day the threads are backed up to GitHub.
    """

    def setUp(self):
        self._plant_decoys()

    def tearDown(self):
        self._clear_decoys()

    def _discover(self):
        uv = shutil.which("uv")
        self.assertIsNotNone(
            uv,
            "PRIVACY FLOOR: `uv` is not on PATH, so the live /local-backup enrolment "
            "could NOT be verified. This is a failure, not a skip: an unverified "
            "enrolment is how private threads end up online.",
        )
        cmd = [
            uv,
            "run",
            "--directory",
            str(BRAIN_ROOT / ".claude" / "scripts"),
            "python",
            "local_backup.py",
            "discover",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(BRAIN_ROOT),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                "PRIVACY FLOOR: `local_backup.py discover` did not finish, so the live "
                "kept-local enrolment could not be verified."
            )
        self.assertEqual(
            proc.returncode,
            0,
            "PRIVACY FLOOR: `local_backup.py discover` failed, so the live kept-local "
            f"enrolment could not be verified.\nstderr: {proc.stderr.strip()}",
        )
        start = proc.stdout.find("{")
        self.assertNotEqual(
            start,
            -1,
            "PRIVACY FLOOR: `local_backup.py discover` printed no JSON, so the live "
            f"kept-local enrolment could not be verified.\nstdout: {proc.stdout[:2000]!r}",
        )
        try:
            payload = json.loads(proc.stdout[start:])
        except json.JSONDecodeError as exc:
            self.fail(
                "PRIVACY FLOOR: `local_backup.py discover` output would not parse, so "
                f"the live kept-local enrolment could not be verified ({exc}).\n"
                f"stdout: {proc.stdout[:2000]!r}"
            )
        home = PLUGIN_HOME.relative_to(BRAIN_ROOT).as_posix()
        for item in payload.get("items", []):
            if item.get("home") == home or item.get("name") == "imessage":
                break
        else:
            self.fail(
                "PRIVACY FLOOR: /local-backup does not know an item named 'imessage', so "
                "NOTHING about this plug-in is enrolled as kept-local. Every thread, the "
                "state file and the ledger would be backed up to GitHub. Items seen: "
                + repr([i.get("name") for i in payload.get("items", [])])
            )
        if item.get("home") not in payload.get("enrolled", []):
            self.skipTest(
                "/local-backup has not enrolled this plug-in, so nothing of it is backed "
                "up. When it is enrolled, keep threads, state.json, ledger.json, "
                "ledger.wal and config.local.json on this machine; this test then checks it."
            )
        return item

    def test_live_record_keeps_all_five_private_paths_local(self):
        item = self._discover()
        kept = {str(entry).lower() for entry in item.get("kept_local", [])}
        for entry in REQUIRED_KEPT_LOCAL:
            self.assertIn(
                entry.lower(),
                kept,
                f"PRIVACY FLOOR: '{entry}' is NOT in the live kept-local record for the "
                f"'imessage' item (recorded: {sorted(kept)}). That path would be copied "
                "into the backup repo and pushed.",
            )

    def test_live_record_actually_withholds_a_file_under_threads(self):
        decoy = PLUGIN_HOME / THREADS_DECOY
        self.assertTrue(
            decoy.exists(),
            f"PRIVACY FLOOR: the decoy {decoy} was not written, so the exclusion was "
            "never put to work.",
        )
        item = self._discover()
        paths = [str(p).replace("\\", "/") for p in item.get("paths", [])]
        leaked = [p for p in paths if "/threads/" in f"/{p}"]
        self.assertEqual(
            leaked,
            [],
            "PRIVACY FLOOR: /local-backup would COPY these files out of threads/ "
            f"despite the kept-local record: {leaked}. The exclusion is recorded but "
            "not doing any work.",
        )
        skipped = {str(entry).lower() for entry in item.get("skipped", {}).get("kept_local", [])}
        self.assertIn(
            "threads",
            skipped,
            "PRIVACY FLOOR: with a real file sitting under threads/, /local-backup did "
            "not report 'threads' as kept-local-skipped (it reported "
            f"{sorted(skipped)}). The exclusion is not being applied to the walk.",
        )


class TestDecoysNeverTouchRealState(unittest.TestCase):
    """The decoys themselves: never overwrite a real file, never delete one.

    Proved on a temporary home, so no real state is ever put at risk to prove it.  The
    race a real run could win is played out directly: the file appears before the
    plant (it is left alone), and the file is replaced or appended to after the plant
    (it is not deleted).
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.mixin = DecoyMixin()
        self.mixin.decoy_home = self.home

    def test_an_existing_file_is_never_overwritten_or_deleted(self):
        real = self.home / "state.json"
        real.write_text('{"watermark": 41}\n', encoding="utf-8")
        self.mixin._plant_decoys()
        self.assertEqual(real.read_text(encoding="utf-8"), '{"watermark": 41}\n')
        self.mixin._clear_decoys()
        self.assertEqual(real.read_text(encoding="utf-8"), '{"watermark": 41}\n')

    def test_a_file_replaced_after_the_plant_is_left_where_it_is(self):
        self.mixin._plant_decoys()
        state, ledger = self.home / "state.json", self.home / "ledger.json"
        self.assertEqual(state.read_text(encoding="utf-8"), DECOY_BODY)
        # a real run: an atomic replace (a NEW inode, even with the decoy's own bytes)…
        swap = self.home / "state.json.tmp"
        swap.write_text(DECOY_BODY, encoding="utf-8")
        os.replace(swap, state)
        # …and an append in place, as the ledger's write-ahead log is written
        with open(self.home / "ledger.wal", "a", encoding="utf-8") as handle:
            handle.write('{"op":"begin","run":"real"}\n')
        self.mixin._clear_decoys()
        self.assertTrue(state.is_file(), "a replaced state.json was deleted")
        self.assertTrue((self.home / "ledger.wal").is_file(), "an appended WAL was deleted")
        self.assertFalse(ledger.exists(), "an untouched decoy was left behind")

    def test_every_untouched_decoy_is_removed_and_nothing_else(self):
        keep = self.home / "threads" / "real thread.txt"
        keep.parent.mkdir()
        keep.write_text("real\n", encoding="utf-8")
        self.mixin._plant_decoys()
        self.mixin._clear_decoys()
        left = sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*"))
        self.assertEqual(left, ["threads", "threads/real thread.txt"])


class TestBrainRepoNeverSeesThisPlugIn(unittest.TestCase):
    """Test 4 — the Brain's own repo never sees anything under _local/."""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not on PATH, so the Brain's view cannot be checked here")
        if not _inside_a_work_tree(BRAIN_ROOT):
            self.skipTest("the Brain folder is not a git work tree, so it can commit nothing")

    def test_brain_status_names_nothing_under_local(self):
        proc = _git("-C", str(BRAIN_ROOT), "status", "--porcelain")
        self.assertEqual(
            proc.returncode,
            0,
            f"PRIVACY FLOOR: `git status` failed in {BRAIN_ROOT}: {proc.stderr.strip()}",
        )
        exposed = [p for p in _status_paths(proc.stdout) if p.startswith("_local/")]
        self.assertEqual(
            exposed,
            [],
            "PRIVACY FLOOR: the Brain's repo can SEE these paths under _local/: "
            f"{exposed}. The Brain's /_local/ ignore has been undone, and this "
            "plug-in's private data would be committed to the Brain.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
