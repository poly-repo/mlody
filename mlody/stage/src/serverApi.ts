import type { ServerHealthStatus, WorkspaceSummary, WorkspaceUser } from "./types.js";

const DEFAULT_SERVER_BASE_URL = "http://127.0.0.1:8765";
const SERVER_TIMEOUT_MS = 8000;

export interface StageBootstrapPayload {
  health: ServerHealthStatus;
  users: WorkspaceUser[];
  workspace: WorkspaceSummary;
}

interface VerbatimExecuteResponse {
  requestId: string;
  command: string;
  status: "done" | "error";
  exitCode: number;
  output: string;
}

export function resolveServerBaseUrl(): string {
  return DEFAULT_SERVER_BASE_URL;
}

function normalizeAvatarUrl(avatar?: string): string | undefined {
  if (!avatar) return undefined;
  if (
    avatar.startsWith("http://") ||
    avatar.startsWith("https://") ||
    avatar.startsWith("/")
  ) {
    return avatar;
  }

  return `/${avatar.replace(/^\.?\//, "")}`;
}

function normalizeUser(payload: unknown): WorkspaceUser | null {
  if (payload === null || typeof payload !== "object") return null;

  const candidate = payload as Record<string, unknown>;
  if (typeof candidate.name !== "string" || candidate.name.trim() === "") {
    return null;
  }

  return {
    name: candidate.name,
    description:
      typeof candidate.description === "string" ? candidate.description : undefined,
    groups: Array.isArray(candidate.groups)
      ? candidate.groups.filter((group): group is string => typeof group === "string")
      : undefined,
    avatar: typeof candidate.avatar === "string" ? candidate.avatar : undefined,
    avatarUrl:
      typeof candidate.avatar === "string"
        ? normalizeAvatarUrl(candidate.avatar)
        : undefined,
  };
}

async function fetchJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`${resolveServerBaseUrl()}${path}`, {
    headers: {
      Accept: "application/json",
    },
    signal,
  });

  if (!response.ok) {
    throw new Error(`${path} failed with status ${response.status}`);
  }

  return (await response.json()) as T;
}

async function postJson<T>(path: string, payload: object): Promise<T> {
  const response = await fetch(`${resolveServerBaseUrl()}${path}`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`${path} failed with status ${response.status}`);
  }

  return (await response.json()) as T;
}

export async function fetchStageBootstrap(
  signal: AbortSignal,
): Promise<StageBootstrapPayload> {
  const health = await fetchJson<ServerHealthStatus>("/healthz", signal);
  if (health.status !== "ok") {
    throw new Error("Server health check did not report ok");
  }

  const [usersPayload, workspace] = await Promise.all([
    fetchJson<unknown[]>("/api/users", signal),
    fetchJson<WorkspaceSummary>("/api/workspace", signal),
  ]);

  return {
    health,
    users: usersPayload
      .map(normalizeUser)
      .filter((user): user is WorkspaceUser => user !== null),
    workspace,
  };
}

export function createServerBootstrapController(): {
  controller: AbortController;
  timeoutId: number;
} {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), SERVER_TIMEOUT_MS);
  return { controller, timeoutId };
}

export async function executeVerbatimCommand(
  command: string,
  input: string,
): Promise<VerbatimExecuteResponse> {
  return await postJson<VerbatimExecuteResponse>("/api/execute/verbatim", {
    command,
    input,
  });
}
