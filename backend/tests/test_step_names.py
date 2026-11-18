"""Issue #12: text must never send anyone to a step that doesn't exist.

The four steps are defined once, in app/steps.py. These tests read every piece of user-facing
text — the dashboard's source (frontend/src), every message string in the backend (not
docstrings), the page templates, and the README — and fail if any of it:
- names a step that isn't in the definition ("Review the page in Preview & publish"),
- gives a step the wrong number ("3. Extract & verify", "Step 5"),
- or types out a step's name instead of taking it from the definition (so a rename can't
  leave an old name behind).
They also check that the dashboard's generated copy (frontend/src/steps.ts) matches.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app import steps

ROOT = steps.TS_PATH.parents[2]           # the checkout: backend/, frontend/, README.md
FRONTEND = ROOT / "frontend" / "src"
BACKEND = Path(steps.__file__).resolve().parent
TITLES = [s.title for s in steps.STEPS]

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="the dashboard's source isn't in this checkout")


# ------------------------------------------------------------------ reading the text
def _strip_ts_comments(src: str) -> str:
    """Blank out // and /* */ comments (JSX {/* */} too), keeping strings and line numbers."""
    out, i, n, quote = [], 0, len(src), None
    while i < n:
        c, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if quote is None:
            if c == "/" and nxt == "/":
                j = src.find("\n", i)
                j = n if j < 0 else j
                out.append(" " * (j - i))
                i = j
                continue
            if c == "/" and nxt == "*":
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append(re.sub(r"[^\n]", " ", src[i:j]))
                i = j
                continue
            if c in "'\"`":
                quote = c
        elif c == "\\":
            out.append(src[i:i + 2])
            i += 2
            continue
        elif c == quote or (c == "\n" and quote in "'\""):   # an apostrophe in JSX text ends at the line
            quote = None
        out.append(c)
        i += 1
    return "".join(out)


def dashboard_text():
    for p in sorted(FRONTEND.rglob("*.ts*")):
        if p == steps.TS_PATH:
            continue
        for no, line in enumerate(_strip_ts_comments(p.read_text()).splitlines(), 1):
            yield f"{p.relative_to(ROOT)}:{no}", line


def backend_text():
    for p in sorted(BACKEND.rglob("*.py")):
        if p.name == "steps.py" and p.parent == BACKEND:
            continue
        tree = ast.parse(p.read_text())
        docstrings = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.JoinedStr):
                yield f"{p.relative_to(BACKEND.parent)}:{n.lineno}", "".join(
                    v.value if isinstance(v, ast.Constant) else "{…}" for v in n.values)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
                yield f"{p.relative_to(BACKEND.parent)}:{n.lineno}", n.value
    for p in sorted(BACKEND.rglob("*.html")):
        for no, line in enumerate(p.read_text().splitlines(), 1):
            yield f"{p.relative_to(BACKEND.parent)}:{no}", line


def doc_text():
    for p in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        if p.is_file():
            for no, line in enumerate(p.read_text().splitlines(), 1):
                yield f"{p.relative_to(ROOT)}:{no}", line


# ------------------------------------------------------------------ spotting a step name
def _alt(names):
    return "|".join("(?:&|&amp;)".join(re.escape(part) for part in x.split("&"))
                    for x in sorted(names, key=len, reverse=True))


_KNOWN = _alt([*TITLES, *steps.FORMER_NAMES])
_NAME = r"[A-Z][A-Za-z]*(?: (?:&|&amp;|and) [a-z]+| [a-z]+)?"      # "Design", "Review page", "Approve & publish"
_B = r"(?:\*\*|<strong>)?"                                          # markdown / html bold around a name
_END = r"(?:\*\*|</strong>)?(?![A-Za-z])(?! [a-z])"                 # the whole name, not the start of a longer one
SPOTS = [
    ("numbered", re.compile(rf"(?<![\w.,{{}}])([1-9])\.\s+{_B}({_KNOWN}){_END}")),     # "4. Approve & publish"
    ("step-number", re.compile(r"\b[Ss]tep ([0-9]+)\b")),                              # "Step 3"
    ("named-step", re.compile(rf"\b(?:the|its|this|your) {_B}({_NAME}?)(?:\*\*|</strong>)? step\b")),  # "its Design step"
    ("next", re.compile(rf"\bNext(?: step)?: {_B}({_NAME}){_END}")),                  # "Next: Design →"
    ("place", re.compile(rf"\b(?:in|from|to|into|open in|back to|go to|continue to) (?:the )?{_B}({_KNOWN}){_END}")),
]


def mentions(text: str):
    """(spot, the words, what's wrong or None) for every place `text` names a step."""
    for spot, rx in SPOTS:
        for m in rx.finditer(text):
            g = [x.replace("&amp;", "&") for x in m.groups()]
            wrong = None
            if spot == "numbered":
                n, name = int(g[0]), g[1]
                if name not in TITLES:
                    wrong = f"there is no step called {name!r}"
                elif TITLES.index(name) + 1 != n:
                    wrong = f"{name!r} is step {TITLES.index(name) + 1}, not {n}"
            elif spot == "step-number":
                if not 1 <= int(g[0]) <= len(TITLES):
                    wrong = f"there are only {len(TITLES)} steps"
            elif g[0] not in TITLES:
                wrong = f"there is no step called {g[0]!r}"
            yield spot, m.group(0).replace("&amp;", "&"), wrong


def problems(lines):
    return [f"{where}: {said!r} — {wrong}" for where, text in lines for _, said, wrong in mentions(text) if wrong]


def typed_out(lines):
    """Step names written into the text instead of taken from app/steps.py (frontend/src/steps.ts)."""
    multiword = re.compile(_alt([t for t in TITLES if " " in t]))
    out = []
    for where, text in lines:
        said = [s for _, s, wrong in mentions(text) if not wrong and any(t in s for t in TITLES)]
        said += [m.group(0) for m in multiword.finditer(text.replace("&amp;", "&"))]
        out += [f"{where}: {s!r}" for s in dict.fromkeys(said)]
    return out


# ------------------------------------------------------------------ the tests
def test_definition_is_well_formed():
    keys = [s.key for s in steps.STEPS]
    assert len(set(keys)) == len(keys) and len(set(TITLES)) == len(TITLES)
    assert not set(TITLES) & set(steps.FORMER_NAMES)
    assert steps.label("publish") == "4. Approve & publish"
    assert steps.after("review").key == "publish" and steps.after("publish") is None


@needs_frontend
def test_dashboard_copy_matches_the_definition():
    assert steps.TS_PATH.read_text() == steps.typescript(), \
        "frontend/src/steps.ts is out of date: run `python -m app.steps` in backend/"


@needs_frontend
def test_dashboard_names_only_real_steps():
    found = problems(dashboard_text())
    assert not found, "\n" + "\n".join(found)


def test_backend_names_only_real_steps():
    found = problems(backend_text())
    assert not found, "\n" + "\n".join(found)


def test_docs_name_only_real_steps():
    found = problems(doc_text())
    assert not found, "\n" + "\n".join(found)


@pytest.mark.skipif(not (ROOT / "README.md").is_file(), reason="no README in this checkout")
def test_readme_lists_the_steps_as_defined():
    """The README can't import the definition, so it must list the steps exactly as defined
    (a renamed step fails here until the README says the new name)."""
    readme = (ROOT / "README.md").read_text()
    missing = [f"{n}. **{s.title}**" for n, s in enumerate(steps.STEPS, 1) if f"{n}. **{s.title}**" not in readme]
    assert not missing, f"README.md's 'The flow' should list {missing}"


@needs_frontend
def test_dashboard_takes_step_names_from_the_definition():
    found = typed_out(dashboard_text())
    assert not found, "\n" + "\n".join(found)


def test_backend_takes_step_names_from_the_definition():
    found = typed_out(backend_text())
    assert not found, "\n" + "\n".join(found)


@pytest.mark.parametrize("text", [
    "No blocking issues. Review the page in <strong>Preview & publish</strong>.",     # issue #12
    "2 blocking issue(s) are open in Validation.",                                     # the publish panel on main
    "3. **Extract & verify**: live progress",                                          # README's old numbering
    "Step 5 of 4",
    "Each report can have its own design, chosen during its Publish step.",
    "Next: Preview →",
])
def test_checker_catches_wrong_step_names(text):
    assert problems([("example", text)])


@pytest.mark.parametrize("text", [
    "No blocking issues. Next step: <strong>4. Approve & publish</strong>.",
    "1 blocking issue is open — resolve it in 1. Extract & verify.",
    "Step 3 of 4",
    "Validation quality",                       # a page's name, not a step
    "Validation issues",
    "4. **Validate** (publishing is blocked while any blocking issue is open)",   # the README's feature list
])
def test_checker_accepts_real_step_names(text):
    assert problems([("example", text)]) == []
