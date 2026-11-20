"""The report wizard's steps: the one place their names and numbers are defined.

Everything that names a step is built from this list, so renaming or re-ordering a step here
changes every mention at once:
- the dashboard reads a generated copy, `frontend/src/steps.ts`; after editing this file run
  `python -m app.steps` from `backend/` (tests/test_step_names.py fails until the copy matches);
- a backend message that names a step uses `label()` / `title()` from here, never a typed name.

tests/test_step_names.py also fails if any user-facing text names a step that isn't here, gives
a step the wrong number, or types a step's name instead of taking it from here.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Step:
    key: str      # the dashboard's ?step= value
    title: str
    help: str     # shown after the title at the top of the step ("Step 3 of 4 Review page — …")


STEPS: tuple[Step, ...] = (
    Step("verify", "Extract & verify", "we read every figure in the PDF and check it; anything flagged needs your decision."),
    Step("design", "Design", "keep your workspace design or choose a new look for this page."),
    Step("review", "Review page", "compare the web page with the PDF before you approve it."),
    Step("publish", "Approve & publish", "approve the page and put it live."),
)

# Names of places that no longer exist (the screen before the four steps had "Preview & publish"
# and "Validation" tabs). Text that sends someone to one of these is wrong.
FORMER_NAMES: tuple[str, ...] = ("Preview & publish", "Preview and publish", "Validation")

_BY_KEY = {s.key: s for s in STEPS}


def number(key: str) -> int:
    return STEPS.index(_BY_KEY[key]) + 1


def title(key: str) -> str:
    return _BY_KEY[key].title


def label(key: str) -> str:
    """How text names a step: "4. Approve & publish"."""
    return f"{number(key)}. {title(key)}"


def after(key: str) -> Step | None:
    i = number(key)
    return STEPS[i] if i < len(STEPS) else None


TS_PATH = Path(__file__).resolve().parents[2] / "frontend" / "src" / "steps.ts"


def typescript() -> str:
    """The dashboard's copy of STEPS (frontend/src/steps.ts)."""
    keys = " | ".join(json.dumps(s.key, ensure_ascii=False) for s in STEPS)
    rows = "\n".join(f"  {{ key: {json.dumps(s.key, ensure_ascii=False)}, number: {i}, title: {json.dumps(s.title, ensure_ascii=False)}, help: {json.dumps(s.help, ensure_ascii=False)} }},"
                     for i, s in enumerate(STEPS, 1))
    return f"""// GENERATED from backend/app/steps.py — do not edit. To change a step, edit that file and run
// `python -m app.steps` in backend/ (backend/tests/test_step_names.py fails until this matches).

export type StepKey = {keys};
export interface StepDef {{ key: StepKey; number: number; title: string; help: string }}

export const STEPS: readonly StepDef[] = [
{rows}
];

export const STEP = Object.fromEntries(STEPS.map((s) => [s.key, s])) as Record<StepKey, StepDef>;

/** How text names a step: "4. Approve & publish". */
export const stepLabel = (key: StepKey): string => `${{STEP[key].number}}. ${{STEP[key].title}}`;

/** The step after this one (undefined after the last). */
export const stepAfter = (key: StepKey): StepDef | undefined => STEPS[STEP[key].number];
"""


if __name__ == "__main__":
    TS_PATH.write_text(typescript())
    print(f"wrote {TS_PATH}", file=sys.stderr)
