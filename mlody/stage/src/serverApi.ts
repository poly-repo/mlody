import type { CommandHistoryEntry } from "./commandHistory.js";
import type {
  StageAutocompletePayload,
  StageCommandLogsPayload,
  ServerHealthStatus,
  StageResultPayload,
  WorkspaceSummary,
  WorkspaceUser,
} from "./types.js";

const DEFAULT_SERVER_BASE_URL = "http://127.0.0.1:8765";
const STANDALONE_STAGE_PORTS = new Set(["8000", "8001"]);
const SERVER_TIMEOUT_MS = 8000;

export interface StageBootstrapPayload {
  health: ServerHealthStatus;
  users: WorkspaceUser[];
  workspace: WorkspaceSummary;
  workspaces: WorkspaceSummary[];
  history: CommandHistoryEntry[];
}

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

export function resolveServerBaseUrl(): string {
  if (typeof window === "undefined") {
    return DEFAULT_SERVER_BASE_URL;
  }

  const explicitOverride = (
    window as typeof window & { __MLODY_SERVER_BASE_URL__?: unknown }
  ).__MLODY_SERVER_BASE_URL__;
  if (typeof explicitOverride === "string" && explicitOverride.trim() !== "") {
    return normalizeBaseUrl(explicitOverride.trim());
  }

  const queryOverride = new URLSearchParams(window.location.search).get(
    "serverBaseUrl",
  );
  if (queryOverride && queryOverride.trim() !== "") {
    return normalizeBaseUrl(queryOverride.trim());
  }

  if (
    window.location.protocol.startsWith("http") &&
    !STANDALONE_STAGE_PORTS.has(window.location.port)
  ) {
    return normalizeBaseUrl(window.location.origin);
  }

  return DEFAULT_SERVER_BASE_URL;
}

function normalizeAvatarUrl(avatar?: string): string | undefined {
  if (!avatar) return undefined;
  if (avatar.startsWith("http://") || avatar.startsWith("https://")) {
    return avatar;
  }

  const normalizedPath = avatar.startsWith("/")
    ? avatar
    : `/${avatar.replace(/^\.?\//, "")}`;

  return new URL(normalizedPath, `${resolveServerBaseUrl()}/`).toString();
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

async function fetchJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${resolveServerBaseUrl()}${path}`, {
    headers: {
      Accept: "application/json",
    },
    signal,
  });

  if (!response.ok) {
    throw new Error(await readErrorMessage(response, path));
  }

  return (await response.json()) as T;
}

async function postJson<T>(
  path: string,
  payload: object,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${resolveServerBaseUrl()}${path}`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok) {
    throw new Error(await readErrorMessage(response, path));
  }

  return (await response.json()) as T;
}

async function readErrorMessage(response: Response, path: string): Promise<string> {
  const fallbackMessage = `${path} failed with status ${response.status}`;

  let rawBody = "";
  try {
    rawBody = await response.text();
  } catch {
    return fallbackMessage;
  }

  if (rawBody.trim() === "") {
    return fallbackMessage;
  }

  try {
    const payload = JSON.parse(rawBody) as Record<string, unknown>;
    if (typeof payload.error === "string" && payload.error.trim() !== "") {
      return payload.error;
    }
  } catch {
    return `${fallbackMessage}: ${rawBody}`;
  }

  return fallbackMessage;
}

export async function fetchStageBootstrap(
  signal: AbortSignal,
): Promise<StageBootstrapPayload> {
  const health = await fetchJson<ServerHealthStatus>("/healthz", signal);
  if (health.status !== "ok") {
    throw new Error("Server health check did not report ok");
  }

  const [usersPayload, workspace, workspacesPayload, history] = await Promise.all([
    fetchJson<unknown[]>("/api/users", signal),
    fetchJson<WorkspaceSummary>("/api/workspace", signal),
    fetchJson<WorkspaceSummary[]>("/api/workspaces", signal).catch(() => null),
    fetchJson<CommandHistoryEntry[]>("/api/history", signal).catch(() => []),
  ]);

  const workspaces =
    Array.isArray(workspacesPayload) && workspacesPayload.length > 0
      ? workspacesPayload
      : [workspace];

  return {
    health,
    history,
    workspaces,
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

export async function executeStageCommand(
  command: string,
  input: string,
  currentUserName: string,
  workspaceRoot: string | null,
): Promise<StageResultPayload> {
  return await postJson<StageResultPayload>("/api/execute/stage", {
    command,
    input,
    options: {
      runAs: currentUserName,
      ...(workspaceRoot ? { workspaceRoot } : {}),
    },
  });
}

export async function fetchStageAutocomplete(
  workspaceRoot: string | null,
  breadcrumb: string[],
  prompt: string,
  signal?: AbortSignal,
): Promise<StageAutocompletePayload> {
  return await postJson<StageAutocompletePayload>(
    "/api/autocomplete/stage",
    {
      workspaceRoot,
      breadcrumb,
      prompt,
    },
    signal,
  );
}

export async function fetchStageCommandLogs(
  requestId: string,
  signal?: AbortSignal,
): Promise<StageCommandLogsPayload> {
  return await fetchJson<StageCommandLogsPayload>(
    `/api/execute/stage/logs/${encodeURIComponent(requestId)}`,
    signal,
  );
}
