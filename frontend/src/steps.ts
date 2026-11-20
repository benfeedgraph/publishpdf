// GENERATED from backend/app/steps.py — do not edit. To change a step, edit that file and run
// `python -m app.steps` in backend/ (backend/tests/test_step_names.py fails until this matches).

export type StepKey = "verify" | "design" | "review" | "publish";
export interface StepDef { key: StepKey; number: number; title: string; help: string }

export const STEPS: readonly StepDef[] = [
  { key: "verify", number: 1, title: "Extract & verify", help: "we read every figure in the PDF and check it; anything flagged needs your decision." },
  { key: "design", number: 2, title: "Design", help: "keep your workspace design or choose a new look for this page." },
  { key: "review", number: 3, title: "Review page", help: "compare the web page with the PDF before you approve it." },
  { key: "publish", number: 4, title: "Approve & publish", help: "approve the page and put it live." },
];

export const STEP = Object.fromEntries(STEPS.map((s) => [s.key, s])) as Record<StepKey, StepDef>;

/** How text names a step: "4. Approve & publish". */
export const stepLabel = (key: StepKey): string => `${STEP[key].number}. ${STEP[key].title}`;

/** The step after this one (undefined after the last). */
export const stepAfter = (key: StepKey): StepDef | undefined => STEPS[STEP[key].number];
