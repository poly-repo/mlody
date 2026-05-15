import type { WorkspaceSummary } from "./types.js";

export const LAUNCH_WORKSPACE_ROOT = "launch-workspace-root" as const;
const LAUNCH_WORKSPACE_PREFIX = `${LAUNCH_WORKSPACE_ROOT}/`;
export const AIRFLOW_SIMPLE_ETL_PIPELINE_WORKSPACE = `${LAUNCH_WORKSPACE_PREFIX}mlody/docs/the-score/sandboxes/airflow-examples/simple-etl-pipeline` as const;

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
      [
        "mav",
        AIRFLOW_SIMPLE_ETL_PIPELINE_WORKSPACE,
        "//pipeline:raw-employees.lineage",
      ],
      [
        "mav",
        AIRFLOW_SIMPLE_ETL_PIPELINE_WORKSPACE,
        "//pipeline:raw-employees-remote._source_range",
      ],
      [
        "mav",
        AIRFLOW_SIMPLE_ETL_PIPELINE_WORKSPACE,
        "//pipeline:raw-employees-remote.location.info",
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
  if (typeof target === "string" && target.startsWith(LAUNCH_WORKSPACE_PREFIX)) {
    const launchWorkspaceRoot = workspace?.workspaceRoot;
    if (!launchWorkspaceRoot) {
      return null;
    }
    const relativePath = target.slice(LAUNCH_WORKSPACE_PREFIX.length);
    return `${launchWorkspaceRoot.replace(/\/$/, "")}/${relativePath}`;
  }

  return target;
}
