import type { WorkspaceSummary } from "./types.js";

export const LAUNCH_WORKSPACE_ROOT = "launch-workspace-root" as const;

export type StageE2eWorkspaceTarget =
  | typeof LAUNCH_WORKSPACE_ROOT
  | string
  | null;

export type StageE2eShowCommand = readonly [
  user: string,
  workspace: StageE2eWorkspaceTarget,
  label: string,
];

export interface StageE2eScenario {
  name: string;
  commands: readonly StageE2eShowCommand[];
}

const STAGE_E2E_TESTS: Record<string, StageE2eScenario> = {
  smoketest: {
    name: "smoketest",
    commands: [
      [
        "mav",
        LAUNCH_WORKSPACE_ROOT,
        "@pixelle//datasets:celebA-dataset.train[@sql select image,Young,Attractive limit 2]",
      ],
    ],
  },
};

export function getStageE2eScenario(name: string): StageE2eScenario | null {
  return STAGE_E2E_TESTS[name] ?? null;
}

export function listStageE2eScenarioNames(): string[] {
  return Object.keys(STAGE_E2E_TESTS).sort((left, right) =>
    left.localeCompare(right),
  );
}

export function resolveStageE2eWorkspaceRoot(
  target: StageE2eWorkspaceTarget,
  workspace: WorkspaceSummary | null,
): string | null {
  if (target === LAUNCH_WORKSPACE_ROOT) {
    return workspace?.workspaceRoot ?? null;
  }

  return target;
}
