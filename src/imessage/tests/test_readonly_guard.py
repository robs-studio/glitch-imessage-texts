"""The read-only guard — proof that this plug-in can only ever READ, and only from here.

Four things are held still by this suite, and each one is a defect that has really
shipped somewhere rather than a hypothetical:

1. **No write path exists to the member's stores.**  ``imchat.py`` and ``imcontacts.py``
   open the Messages database and the macOS address book.  Neither may contain write
   vocabulary, neither may open a store read-write, neither may use ``immutable=1``, and
   **every** ``sqlite3.connect`` in them must pass ``mode=ro``.  A guard that only read the
   docstring's promise would be a promise; this one reads the code.
2. **The import order is right in a REAL process.**  An in-process assertion cannot see
   the true ``sys.path`` order, because the test file has already put the plug-in home in
   front before the first import — so the guard runs in a fresh interpreter.  The bug it
   exists to stop has shipped in a sibling plug-in, and is rebuilt here in a temp folder as
   a negative control: a config module that puts the engine's scripts folder first and
   stops, after which ``import shared`` yields the ENGINE's ``shared.py`` over the
   plug-in's own, silently.
3. **``check`` answers the same from every working directory.**  ``imessage.py`` claims
   cwd-independence; it is run from the Brain root, from ``.claude/scripts`` and from the
   plug-in folder, and the paths it reports are compared.  One invocation proves nothing.
4. **The one file this plug-in writes is written safely.**  ``config.local.json`` may
   already hold ``never_ingest`` — the handles whose conversations are never read at all.
   The writer must refuse a target outside the plug-in folder, merge rather than clobber,
   land ``0600``, and leave NOTHING behind when serialisation fails.

Two rules this file is written under
-------------------------------------
**Nothing here may print a real name, a real number or a real address.**  Every fixture
handle is invented: the ``555-01xx`` range is the reserved fictional block and the domains
are ``example.com`` / ``example.org`` (RFC 2606).  The one subprocess that touches real
data is run with ``--mask``, so what comes back carries shapes, never values.

**The member's real ``config.local.json`` is never written.**  Every write in this file
goes to a temp directory, and :func:`tearDownModule` compares the real file byte-for-byte
against how it was found — a test that wrote it would fail the whole module, loudly, even
if every assertion in it passed.

How each assertion is made, since the brief asks
-------------------------------------------------
Parsing is ``ast`` wherever the question is structural: which strings are code rather than
documentation, which calls are ``sqlite3.connect``, what could reach that call's first
argument.  ``tokenize`` does the one job ``ast`` cannot — handing back the source with
every comment and every string literal removed — so the bare-token scan runs over code and
cannot be tripped by prose.  No regex is used to understand Python anywhere in this file;
a regex cannot tell a ``#`` inside a string from a comment, and this guard's whole job is
to be un-foolable.  The prose carve-out is deliberate and is pinned by its own control
(:class:`TestSourceGuardBites`): ``imchat``'s docstring says ``immutable=1`` is forbidden,
and a guard that red-flagged the sentence explaining the rule would be deleted within a
week, which is how guards die.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own modules first, before anything reachable only because imconfig
# put `.claude/scripts` on sys.path. This suite imports no engine module at all, so
# the ordering never comes down to test-discovery luck.
import imconfig  # noqa: E402
import imessage  # noqa: E402

import ast  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import tokenize  # noqa: E402
import unittest  # noqa: E402

BRAIN_ROOT = PLUGIN_HOME.parents[1]
ENGINE_SCRIPTS = Path(imconfig.SCRIPTS_DIR).resolve()

#: The two modules that open one of the member's stores. Both are audited; the CLI
#: is not, because it opens nothing — it goes through imchat.
GUARDED_MODULES = ("imchat", "imcontacts")

#: SQL that changes something. Checked case-sensitively, because this codebase
#: writes SQL keywords in upper case and a lower-case "delete" in code is English.
WRITE_TOKENS = ("INSERT", "UPDATE", "DELETE", "DROP", "PRAGMA journal")

#: Every way of opening a SQLite store that is not read-only, plus the one
#: read-only-looking flag that is forbidden here: `immutable=1` lets SQLite ignore
#: the write-ahead log, which on a live store silently costs the newest messages.
FORBIDDEN_OPEN_FLAGS = ("immutable=1", "mode=rw", "mode=rwc", "mode=memory")

#: What every connect in a guarded module must carry.
REQUIRED_OPEN_FLAG = "mode=ro"

# Invented throughout. 555-01xx is the reserved fictional block; example.com and
# example.org are RFC 2606.
FIXTURE_OWN = ["+15555550101", "you@example.com"]
FIXTURE_NEVER = ["+15555550199", "someone@example.org"]


# ---------------------------------------------------------------------------
# The real config.local.json is NEVER written by this suite. Proven, not assumed.
# ---------------------------------------------------------------------------

_REAL_LOCAL = Path(imconfig.LOCAL_CONFIG_PATH)
_REAL_LOCAL_BEFORE: bytes | None = None


def setUpModule():
    global _REAL_LOCAL_BEFORE
    _REAL_LOCAL_BEFORE = _REAL_LOCAL.read_bytes() if _REAL_LOCAL.is_file() else None


def tearDownModule():
    after = _REAL_LOCAL.read_bytes() if _REAL_LOCAL.is_file() else None
    if after != _REAL_LOCAL_BEFORE:
        raise AssertionError(
            f"THIS SUITE WROTE THE MEMBER'S REAL {_REAL_LOCAL}. Every write in these "
            "tests must go to a temp directory; that file holds never_ingest, which is "
            "the list of people whose conversations must never be read."
        )


# ---------------------------------------------------------------------------
# Test 1 — the source guard. ast for structure, tokenize for the prose carve-out.
# ---------------------------------------------------------------------------


def _parse(name: str, source: str) -> dict:
    """One module's AST plus its module-level names, ready for the audit."""
    tree = ast.parse(source, filename=f"{name}.py")
    funcs: dict[str, ast.AST] = {}
    assigns: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigns[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assigns[node.target.id] = node.value
    return {"name": name, "source": source, "tree": tree, "funcs": funcs, "assigns": assigns}


def _documentation_constants(tree: ast.AST) -> set[int]:
    """The ids of every string Constant that is documentation, not code.

    A bare string STATEMENT is the only way a string appears without being used,
    and that is exactly what a docstring is — module, class, function, or the
    attribute-doc convention.  ``ast`` answers this exactly; nothing else does.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _code_strings(tree: ast.AST) -> list[str]:
    """Every string literal in the module that is NOT documentation. ast."""
    docs = _documentation_constants(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docs
    ]


def _code_only_text(source: str) -> str:
    """The source with every comment and every string literal removed. tokenize.

    This is the one job ``ast`` cannot do, and a regex must not: only the tokenizer
    knows that a ``#`` inside a string is not a comment and that a quote inside a
    comment does not open a string.  What comes back is the executable skeleton, so
    a bare-token scan over it cannot be tripped by a docstring that merely *names*
    a forbidden thing — which ``imchat``'s docstring deliberately does.
    """
    skip = {
        tokenize.COMMENT,
        tokenize.STRING,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
    # Python 3.12+ splits f-strings into their own token types; the MIDDLE pieces
    # are literal text and belong with the strings. getattr keeps this working on
    # every version rather than pinning one.
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        token_type = getattr(tokenize, name, None)
        if token_type is not None:
            skip.add(token_type)

    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in skip:
            continue
        kept.append(token.string)
    return " ".join(kept)


def _connect_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``sqlite3.connect(...)`` call node. ast."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
    ]


def _returns(function: ast.AST) -> list[ast.AST]:
    return [node.value for node in ast.walk(function) if isinstance(node, ast.Return) and node.value]


def _reachable_literals(expr, module: dict, modules: dict, seen=None) -> list[str]:
    """Every string literal that could end up in ``expr``. ast, following names.

    ``sqlite3.connect(_read_only_uri(store), uri=True)`` does not carry ``mode=ro``
    anywhere near the call, so a guard that only looked at the call site would
    have to either fail a correct module or pass anything.  This walks the
    expression, and through it into module-level constants and the return
    expressions of module-level helpers — including a helper reached through
    another guarded module — until it finds the literals that can actually arrive.
    ``seen`` stops a recursive helper from looping.
    """
    if seen is None:
        seen = set()
    out: list[str] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.Name):
            key = (module["name"], node.id)
            if key in seen:
                continue
            seen.add(key)
            target = module["assigns"].get(node.id)
            if target is not None:
                out += _reachable_literals(target, module, modules, seen)
            helper = module["funcs"].get(node.id)
            if helper is not None:
                for value in _returns(helper):
                    out += _reachable_literals(value, module, modules, seen)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            other = modules.get(node.value.id)
            if other is None:
                continue
            key = (other["name"], node.attr)
            if key in seen:
                continue
            seen.add(key)
            target = other["assigns"].get(node.attr)
            if target is not None:
                out += _reachable_literals(target, other, modules, seen)
            helper = other["funcs"].get(node.attr)
            if helper is not None:
                for value in _returns(helper):
                    out += _reachable_literals(value, other, modules, seen)
    return out


def audit_module(module: dict, modules: dict) -> list[str]:
    """Every read-only violation in one module, in plain words. Empty means clean.

    ONE function, used by the real test and by every negative control, so a
    control can never prove a look-alike written next to the real assertion.
    """
    problems: list[str] = []
    name = module["name"]
    tree = module["tree"]

    # (a) write vocabulary in a string that is CODE (ast).
    for text in _code_strings(tree):
        for token in WRITE_TOKENS:
            if token in text:
                problems.append(
                    f"{name}.py: the string {text[:60]!r} carries '{token}'. This plug-in "
                    "never changes a member's stores."
                )

    # (b) write vocabulary anywhere in the executable skeleton (tokenize).
    skeleton = _code_only_text(module["source"])
    for token in WRITE_TOKENS:
        if token in skeleton:
            problems.append(
                f"{name}.py: '{token}' appears in the module's code (not in a comment or "
                "a docstring). This plug-in never changes a member's stores."
            )

    # (c) a store opened any way but read-only (ast for strings, tokenize for the rest).
    for text in _code_strings(tree):
        for flag in FORBIDDEN_OPEN_FLAGS:
            if flag in text:
                problems.append(
                    f"{name}.py: the string {text[:60]!r} carries '{flag}'. A store here is "
                    "opened read-only and nothing else; immutable=1 additionally loses the "
                    "newest messages by ignoring the write-ahead log."
                )

    # (d) sqlite3 must not be imported piecemeal, or the audit below can be dodged.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            problems.append(
                f"{name}.py: `from sqlite3 import ...` hides the connect call from this "
                "guard. Import the module and call sqlite3.connect(...)."
            )

    # (e) EVERY connect must be a read-only URI open (ast).
    calls = _connect_calls(tree)
    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        uri = keywords.get("uri")
        if not (isinstance(uri, ast.Constant) and uri.value is True):
            problems.append(
                f"{name}.py line {call.lineno}: sqlite3.connect(...) without uri=True. "
                "mode=ro only takes effect on a URI open, so this opens read-write."
            )
        if not call.args:
            problems.append(
                f"{name}.py line {call.lineno}: sqlite3.connect(...) with no path argument."
            )
            continue
        literals = _reachable_literals(call.args[0], module, modules)
        if not any(REQUIRED_OPEN_FLAG in text for text in literals):
            problems.append(
                f"{name}.py line {call.lineno}: nothing that can reach this "
                f"sqlite3.connect(...) carries '{REQUIRED_OPEN_FLAG}' (found: "
                f"{sorted(set(literals))!r}). A store here is opened read-only, always. "
                "Accepted shapes: a literal URI, an f-string, or a module-level constant "
                "or helper whose return carries the flag."
            )
    return problems


def _load_guarded_modules(case) -> dict:
    """Parse every guarded module, FAILING loudly if one is not on disk.

    A missing module is a failure, never a skip.  A silently-skipped guard is how
    a write path ships: the suite stays green, the file lands later, and nothing
    ever looks at it again.
    """
    modules = {}
    for name in GUARDED_MODULES:
        path = PLUGIN_HOME / f"{name}.py"
        case.assertTrue(
            path.is_file(),
            f"READ-ONLY GUARD: {path} does not exist, so NOTHING about how this plug-in "
            "opens a member's stores has been checked. This is a failure, not a skip.",
        )
        modules[name] = _parse(name, path.read_text(encoding="utf-8"))
    return modules


class TestGuardedModulesAreReadOnly(unittest.TestCase):
    """Test 1 — imchat.py and imcontacts.py can only ever read."""

    def test_no_guarded_module_can_write(self):
        modules = _load_guarded_modules(self)
        for name, module in modules.items():
            with self.subTest(module=name):
                problems = audit_module(module, modules)
                self.assertEqual(
                    problems,
                    [],
                    "READ-ONLY GUARD: " + "\n  ".join(problems),
                )

    def test_every_guarded_module_really_opens_something(self):
        """Without this, a module that opened nothing would pass (e) vacuously."""
        modules = _load_guarded_modules(self)
        for name, module in modules.items():
            with self.subTest(module=name):
                self.assertGreater(
                    len(_connect_calls(module["tree"])),
                    0,
                    f"READ-ONLY GUARD: {name}.py contains no sqlite3.connect call, so the "
                    "read-only audit above checked nothing at all. Either the module no "
                    "longer opens a store (remove it from GUARDED_MODULES) or it opens "
                    "one some other way, which this guard cannot see.",
                )


class TestSourceGuardBites(unittest.TestCase):
    """The negative controls for Test 1 — each clause proven to fail on a real violation.

    Every control runs the SAME :func:`audit_module` the real test runs, over a tiny
    synthetic module.  The last one runs the opposite way round: prose that merely
    NAMES a forbidden thing must stay clean, because ``imchat``'s own docstring does
    exactly that and a guard that punished it would be switched off.
    """

    VIOLATIONS = {
        "a delete statement in code": (
            "import sqlite3\n"
            "def f(c):\n"
            "    c.execute('DELETE FROM message')\n"
        ),
        "a write hidden behind a module-level name": (
            "import sqlite3\n"
            "SQL = 'UPDATE chat SET display_name = ?'\n"
            "def f(c):\n"
            "    c.execute(SQL)\n"
        ),
        "an immutable open": (
            "import sqlite3\n"
            "def f(p):\n"
            "    return sqlite3.connect(p.as_uri() + '?immutable=1', uri=True)\n"
        ),
        "a read-write open": (
            "import sqlite3\n"
            "def f(p):\n"
            "    return sqlite3.connect(p.as_uri() + '?mode=rwc', uri=True)\n"
        ),
        "a plain path open, no URI at all": (
            "import sqlite3\n"
            "def f(p):\n"
            "    return sqlite3.connect(str(p))\n"
        ),
        "a uri open with no mode at all": (
            "import sqlite3\n"
            "def f(p):\n"
            "    return sqlite3.connect(p.as_uri(), uri=True)\n"
        ),
        "connect imported piecemeal to dodge the audit": (
            "from sqlite3 import connect\n"
            "def f(p):\n"
            "    return connect(str(p))\n"
        ),
        "a read-only helper quietly changed to read-write": (
            "import sqlite3\n"
            "def _uri(p):\n"
            "    return p.as_uri() + '?mode=rw'\n"
            "def f(p):\n"
            "    return sqlite3.connect(_uri(p), uri=True)\n"
        ),
    }

    CLEAN = {
        "a docstring that names the forbidden things": (
            '"""This module never runs an INSERT, an UPDATE or a DELETE, and immutable=1\n'
            'is forbidden here because it would ignore the write-ahead log."""\n'
            "import sqlite3\n"
            "# DROP TABLE would be a disaster; mode=rwc likewise.\n"
            "def f(p):\n"
            "    return sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=5.0)\n"
        ),
        "a read-only open through a module-level helper": (
            "import sqlite3\n"
            "def _uri(p):\n"
            "    return f'{p.as_uri()}?mode=ro'\n"
            "def f(p):\n"
            "    return sqlite3.connect(_uri(p), uri=True)\n"
        ),
    }

    def test_every_violation_is_caught(self):
        for label, source in self.VIOLATIONS.items():
            with self.subTest(violation=label):
                module = _parse("fixture", source)
                problems = audit_module(module, {"fixture": module})
                self.assertNotEqual(
                    problems,
                    [],
                    f"READ-ONLY GUARD IS BLIND: '{label}' produced no complaint, so the "
                    "audit in Test 1 proves nothing about that kind of write path.",
                )

    def test_clean_sources_stay_clean(self):
        for label, source in self.CLEAN.items():
            with self.subTest(clean=label):
                module = _parse("fixture", source)
                problems = audit_module(module, {"fixture": module})
                self.assertEqual(
                    problems,
                    [],
                    f"READ-ONLY GUARD IS TOO LOUD: '{label}' is correct code and was "
                    f"flagged anyway: {problems}. A guard that reds a module's own "
                    "explanation of the rule gets switched off, and then it guards nothing.",
                )


# ---------------------------------------------------------------------------
# Test 2 — the sys.path ORDER, in a REAL process.
# ---------------------------------------------------------------------------

_PROBE = (
    "import sys, json\n"
    "sys.path.insert(0, {home!r})\n"
    "import {config_module}\n"
    "mod = __import__({engine_module!r})\n"
    "print(json.dumps({{'path0': sys.path[0], "
    "'resolved': getattr(mod, '__file__', None)}}))\n"
)


def _run_probe(case, home, config_module, engine_module):
    """A fresh interpreter, a neutral cwd, no inherited PYTHONPATH.

    In-process this question cannot be asked at all: this file put the plug-in home
    on ``sys.path`` before its first import, so the order under test is already
    decided.  The ``sys.path.insert`` in the probe is exactly what running a script
    out of that folder gives you, and nothing else is pre-arranged.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    code = _PROBE.format(
        home=str(home), config_module=config_module, engine_module=engine_module
    )
    with tempfile.TemporaryDirectory() as neutral:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=neutral,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    case.assertEqual(
        proc.returncode,
        0,
        f"IMPORT ORDER: the probe for {config_module}/{engine_module} did not run.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}",
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    case.assertTrue(
        lines,
        f"IMPORT ORDER: the probe printed no result.\nstdout: {proc.stdout!r}",
    )
    return json.loads(lines[-1])


class TestImportOrderInARealProcess(unittest.TestCase):
    """Test 2 — imconfig leaves THIS folder ahead of the engine, and the engine importable."""

    def test_plugin_home_wins_and_the_engine_still_resolves(self):
        result = _run_probe(self, PLUGIN_HOME, "imconfig", "shared")

        self.assertEqual(
            Path(result["path0"]).resolve(),
            PLUGIN_HOME,
            "IMPORT ORDER: after `import imconfig`, sys.path[0] is "
            f"{result['path0']!r}, not the plug-in home {PLUGIN_HOME}. The engine is "
            "ahead of this plug-in, so any module here whose name an engine module also "
            "uses is silently replaced by the engine's. This is the shadowing bug a "
            "sibling plug-in shipped; imconfig.ensure_engine_path's step 2 is what "
            "prevents it.",
        )

        resolved = result["resolved"]
        self.assertIsNotNone(
            resolved,
            "IMPORT ORDER: the engine module `shared` imported but has no __file__.",
        )
        self.assertEqual(
            Path(resolved).resolve(),
            ENGINE_SCRIPTS / "shared.py",
            f"IMPORT ORDER: `import shared` resolved to {resolved!r}, not "
            f"{ENGINE_SCRIPTS / 'shared.py'}. Putting this plug-in first must not cost "
            "the engine's own modules.",
        )


#: The known-bad config, as a sibling plug-in once shipped it: the engine's scripts
#: folder inserted at index 0, and nothing after it to put the plug-in back in front.
_KNOWN_BAD_CONFIG = "import sys\nsys.path.insert(0, {scripts!r})\n"


class TestImportOrderGuardBites(unittest.TestCase):
    """The negative control for Test 2 — the SAME probe, on a plug-in that has the bug.

    A temp folder stands in for a plug-in whose config module inserts the engine's
    scripts folder at index 0 and stops, leaving the ENGINE ahead of the plug-in.
    The folder has its own ``shared.py`` and so does the engine, so the wrong one
    wins — nothing raises, the plug-in just runs someone else's code.  Reproducing
    it here, on any machine, is what proves the assertions above can fail.
    """

    def setUp(self):
        if not (ENGINE_SCRIPTS / "shared.py").is_file():
            self.skipTest("the engine has no shared.py, so there is no name to collide")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bad_home = Path(tmp.name).resolve() / "known-bad-plugin"
        self.bad_home.mkdir()
        (self.bad_home / "badconfig.py").write_text(
            _KNOWN_BAD_CONFIG.format(scripts=str(ENGINE_SCRIPTS)), encoding="utf-8"
        )
        (self.bad_home / "shared.py").write_text(
            '"""The plug-in\'s own module, which the engine\'s must not replace."""\n',
            encoding="utf-8",
        )

    def test_the_known_bad_plugin_really_loses_to_the_engine(self):
        result = _run_probe(self, self.bad_home, "badconfig", "shared")

        self.assertEqual(
            Path(result["path0"]).resolve(),
            ENGINE_SCRIPTS,
            "IMPORT ORDER CONTROL: the known-bad config did not put the engine ahead of "
            f"its plug-in (sys.path[0] was {result['path0']!r}), so the probe is not "
            "measuring what it claims.",
        )
        self.assertEqual(
            Path(result["resolved"]).resolve(),
            ENGINE_SCRIPTS / "shared.py",
            "IMPORT ORDER CONTROL: `import shared` after the known-bad config did not "
            f"resolve to the engine's copy ({result['resolved']!r}), so the shadowing bug "
            "this guard exists to prevent could not be reproduced and the assertions in "
            "TestImportOrderInARealProcess are unproven.",
        )
        self.assertNotEqual(
            Path(result["resolved"]).resolve(),
            (self.bad_home / "shared.py").resolve(),
            "IMPORT ORDER CONTROL: the known-bad plug-in got its own shared.py after all.",
        )


# ---------------------------------------------------------------------------
# Test 3 — `check` answers the same from every working directory.
# ---------------------------------------------------------------------------

#: The three places this plug-in is entered from: the Brain root (a member CLI),
#: `.claude/scripts` (where `uv run --directory` lands), and the plug-in folder
#: itself (the morning stage).
WORKING_DIRECTORIES = (BRAIN_ROOT, ENGINE_SCRIPTS, PLUGIN_HOME)


def _run_check(case, cwd):
    """`imessage.py check --json --mask` from ``cwd``. --mask: the output is real data."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_HOME / "imessage.py"), "check", "--json", "--mask"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    case.assertEqual(
        proc.returncode,
        0,
        f"CWD INDEPENDENCE: `check` exited {proc.returncode} from {cwd}. It is a "
        "read-only diagnostic and must exit 0 on every expected condition, including no "
        f"access at all.\nstdout: {proc.stdout[:2000]!r}\nstderr: {proc.stderr[:2000]!r}",
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        case.fail(
            f"CWD INDEPENDENCE: --json output from {cwd} would not parse ({exc}). "
            "Diagnostics belong on stderr so stdout stays machine-readable.\n"
            f"stdout: {proc.stdout[:2000]!r}"
        )


class TestCheckIsWorkingDirectoryIndependent(unittest.TestCase):
    """Test 3 — the same resolved paths from the Brain root, from scripts/, and from here."""

    def test_the_three_working_directories_are_really_different(self):
        """Without this, Test 3 could be comparing one directory with itself."""
        resolved = {str(Path(d).resolve()) for d in WORKING_DIRECTORIES}
        self.assertEqual(
            len(resolved),
            3,
            "CWD INDEPENDENCE: the three working directories are not three distinct "
            f"places ({sorted(resolved)}), so the comparison below proves nothing.",
        )

    def test_every_working_directory_reports_identical_paths(self):
        reports = {str(d): _run_check(self, d) for d in WORKING_DIRECTORIES}

        first_cwd, first = next(iter(reports.items()))
        for cwd, report in reports.items():
            with self.subTest(cwd=cwd):
                self.assertEqual(
                    report["paths"],
                    first["paths"],
                    "CWD INDEPENDENCE: `check` run from "
                    f"{cwd} resolved different paths than the same command run from "
                    f"{first_cwd}.\n  from {cwd}: {report['paths']}\n"
                    f"  from {first_cwd}: {first['paths']}\n"
                    "Something in the plug-in is reading Path.cwd() instead of __file__; "
                    "the failure that causes is a ledger written in one entry point and "
                    "read in another, which is silent duplicate ingestion, not a crash.",
                )

        self.assertEqual(
            Path(first["paths"]["plugin_home"]).resolve(),
            PLUGIN_HOME,
            "CWD INDEPENDENCE: `check` agrees with itself but points at "
            f"{first['paths']['plugin_home']!r}, which is not this plug-in.",
        )
        self.assertEqual(
            Path(first["paths"]["user_profile"]).resolve(),
            (BRAIN_ROOT / "glitch-mem" / "Memory" / "USER.md").resolve(),
            "CWD INDEPENDENCE: the member's profile resolved somewhere unexpected: "
            f"{first['paths']['user_profile']!r}",
        )


# ---------------------------------------------------------------------------
# Test 4 — the config.local.json write guard.
# ---------------------------------------------------------------------------


def _assert_only(case, folder: Path, expected: set[str], why: str):
    """``folder`` holds exactly ``expected`` and nothing else — no stray temp file.

    ONE helper, used by the real assertions and by the control that proves it bites.
    """
    actual = {p.name for p in Path(folder).iterdir()}
    case.assertEqual(
        actual,
        expected,
        f"{why}\n  expected exactly {sorted(expected)}\n  found {sorted(actual)}",
    )


def _assert_kept(case, payload: dict, why: str):
    """The settings a write must never silently drop are all still there.

    ONE helper, used by the real assertion and by the control that proves it bites.
    """
    case.assertEqual(payload.get("never_ingest"), FIXTURE_NEVER, why)
    case.assertEqual(payload.get("synth_stale_days"), 5, why)
    case.assertEqual(payload.get("_readme"), "mine, keep it", why)


def _assert_owner_only(case, path: Path, why: str):
    """Mode 0600 where the OS means it. ONE helper; its control proves it bites."""
    if os.name == "nt":
        case.skipTest("POSIX modes are advisory on Windows")
    case.assertEqual(
        stat.S_IMODE(path.stat().st_mode),
        0o600,
        f"{why} (mode is {stat.S_IMODE(path.stat().st_mode):04o})",
    )


class LocalConfigWriteCase(unittest.TestCase):
    """A temp directory that stands in for the plug-in home. The real one is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "imessage"
        self.home.mkdir()
        self.outside = Path(self._tmp.name) / "somewhere-else"
        self.outside.mkdir()
        self.target = self.home / "config.local.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_existing(self):
        """A config.local.json that already holds the settings a write must not lose."""
        self.target.write_text(
            json.dumps(
                {
                    "_readme": "mine, keep it",
                    "never_ingest": FIXTURE_NEVER,
                    "synth_stale_days": 5,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


class TestLocalConfigWriteRefusesForeignTargets(LocalConfigWriteCase):
    """Test 4a — the writer writes inside the plug-in folder and nowhere else."""

    def test_a_target_outside_the_plugin_folder_is_refused(self):
        stray = self.outside / "config.local.json"
        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.write_local_config(FIXTURE_OWN, target=stray, home=self.home)
        self.assertFalse(
            stray.exists(),
            "WRITE GUARD: the write was refused but the file appeared anyway.",
        )
        _assert_only(
            self,
            self.outside,
            set(),
            "WRITE GUARD: a refused write left something behind outside the plug-in folder.",
        )

    def test_the_real_plugin_home_refuses_a_temp_target(self):
        """The default home must refuse a target elsewhere, not just a temp home."""
        stray = self.outside / "config.local.json"
        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.write_local_config(FIXTURE_OWN, target=stray)
        self.assertFalse(stray.exists())

    def test_a_symlinked_spelling_of_the_same_folder_is_accepted(self):
        """Proves BOTH sides are resolved — the macOS temp tree is symlinked.

        ``home`` is given by one spelling and ``target`` by another, and they are the
        same directory.  A comparison that did not resolve both sides would call
        these different folders and refuse a perfectly legitimate write; spelled the
        other way round, the same unresolved comparison is what lets a foreign write
        through.  This is the clause that bites either way.
        """
        link = Path(self._tmp.name) / "home-by-another-name"
        try:
            link.symlink_to(self.home, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this filesystem does not support symlinks")

        written, merged = imessage.write_local_config(
            FIXTURE_OWN, target=link / "config.local.json", home=self.home
        )
        self.assertEqual(merged["own_handles"], FIXTURE_OWN)
        self.assertTrue(
            self.target.is_file(),
            f"WRITE GUARD: the write reported success at {written} but nothing landed in "
            f"{self.home}.",
        )

    def test_a_symlink_out_of_the_plugin_folder_is_refused(self):
        """A link planted INSIDE the folder is judged on where it really lands."""
        escape = self.home / "escape"
        try:
            escape.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this filesystem does not support symlinks")

        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.write_local_config(
                FIXTURE_OWN, target=escape / "config.local.json", home=self.home
            )
        _assert_only(
            self,
            self.outside,
            set(),
            "WRITE GUARD: a write escaped the plug-in folder through a symlink.",
        )


class TestLocalConfigWriteMerges(LocalConfigWriteCase):
    """Test 4b — never_ingest survives. This is the most sensitive value in the build."""

    def test_an_existing_never_ingest_survives_the_write(self):
        self._seed_existing()
        _written, merged = imessage.write_local_config(
            FIXTURE_OWN, target=self.target, home=self.home
        )

        on_disk = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["own_handles"], FIXTURE_OWN)
        _assert_kept(
            self,
            on_disk,
            "WRITE GUARD: the write CLOBBERED config.local.json. never_ingest is the list "
            "of people whose conversations must never be read; losing it silently switches "
            "that protection off and nothing downstream notices.",
        )
        self.assertEqual(merged, on_disk, "WRITE GUARD: the return value is not what landed.")

    def test_a_fresh_file_is_created_with_a_readme_and_the_handles(self):
        _written, merged = imessage.write_local_config(
            FIXTURE_OWN, target=self.target, home=self.home
        )
        on_disk = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["own_handles"], FIXTURE_OWN)
        self.assertIn("_readme", on_disk)
        self.assertEqual(merged, on_disk)

    def test_the_written_file_is_owner_only(self):
        imessage.write_local_config(FIXTURE_OWN, target=self.target, home=self.home)
        _assert_owner_only(
            self,
            self.target,
            "WRITE GUARD: config.local.json is not owner-only. It holds the member's own "
            "numbers and the list of people never to read.",
        )

    def test_an_existing_loose_file_is_tightened(self):
        self._seed_existing()
        os.chmod(self.target, 0o644)
        imessage.write_local_config(FIXTURE_OWN, target=self.target, home=self.home)
        _assert_owner_only(
            self,
            self.target,
            "WRITE GUARD: a file left loose by an earlier tool kept its mode through a "
            "write instead of being tightened.",
        )

    def test_imconfig_reads_back_exactly_what_was_written(self):
        """The writer and the reader are one round trip, not two opinions."""
        self._seed_existing()
        imessage.write_local_config(FIXTURE_OWN, target=self.target, home=self.home)
        cfg = imconfig.load_config(
            path=self.home / "no-such-config.json", local_path=self.target
        )
        self.assertEqual(imconfig.require_own_handles(cfg), FIXTURE_OWN)
        self.assertEqual(cfg["never_ingest"], FIXTURE_NEVER)

    def test_an_empty_list_is_refused(self):
        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.write_local_config([], target=self.target, home=self.home)
        self.assertFalse(self.target.exists())

    def test_a_corrupt_existing_file_is_refused_rather_than_merged_onto_nothing(self):
        self.target.write_text('{"never_ingest": ["+1555555019', encoding="utf-8")
        before = self.target.read_bytes()
        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.write_local_config(FIXTURE_OWN, target=self.target, home=self.home)
        self.assertEqual(
            self.target.read_bytes(),
            before,
            "WRITE GUARD: a corrupt config.local.json was overwritten. Treating it as "
            "empty would silently replace never_ingest with nothing.",
        )
        _assert_only(
            self,
            self.home,
            {"config.local.json"},
            "WRITE GUARD: a refused write left a temp file behind.",
        )


class TestLocalConfigWriteIsAtomic(LocalConfigWriteCase):
    """Test 4c — a payload that cannot be serialised leaves NOTHING on disk."""

    def test_a_serialisation_failure_leaves_no_file_at_all(self):
        with self.assertRaises((TypeError, ValueError)):
            imessage.write_local_config([object()], target=self.target, home=self.home)
        self.assertFalse(
            self.target.exists(),
            "WRITE GUARD: a payload that could not be serialised still created "
            "config.local.json.",
        )
        _assert_only(
            self,
            self.home,
            set(),
            "WRITE GUARD: a failed write left a partial or temp file behind. Serialising "
            "must happen BEFORE the temp file is created.",
        )

    def test_a_serialisation_failure_does_not_touch_an_existing_file(self):
        self._seed_existing()
        before = self.target.read_bytes()
        with self.assertRaises((TypeError, ValueError)):
            imessage.write_local_config([object()], target=self.target, home=self.home)
        self.assertEqual(
            self.target.read_bytes(),
            before,
            "WRITE GUARD: a failed write changed the existing config.local.json.",
        )
        _assert_only(
            self,
            self.home,
            {"config.local.json"},
            "WRITE GUARD: a failed write left a temp file beside the real one.",
        )


class TestWriteGuardBites(LocalConfigWriteCase):
    """The negative controls for Test 4 — every helper proven to fail on the real defect.

    Each control feeds the SAME helper the real tests use, so a control can never end
    up proving a look-alike written next to the assertion it is supposed to protect.
    """

    def test_the_stray_file_helper_catches_a_stray_file(self):
        (self.home / "config.local.json.abc123.tmp").write_text("half a file", encoding="utf-8")
        with self.assertRaises(AssertionError):
            _assert_only(self, self.home, set(), "control")

    def test_the_merge_helper_catches_a_clobbering_write(self):
        """A naive writer — the one this guard exists to stop — must fail the helper."""
        self._seed_existing()
        self.target.write_text(
            json.dumps({"own_handles": FIXTURE_OWN}, indent=2) + "\n", encoding="utf-8"
        )
        clobbered = json.loads(self.target.read_text(encoding="utf-8"))
        with self.assertRaises(AssertionError):
            _assert_kept(self, clobbered, "control")

    def test_the_owner_only_helper_catches_a_loose_file(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are advisory on Windows")
        self.target.write_text("{}", encoding="utf-8")
        os.chmod(self.target, 0o644)
        with self.assertRaises(AssertionError):
            _assert_owner_only(self, self.target, "control")


if __name__ == "__main__":
    unittest.main(verbosity=2)
