import { useEffect, useState } from "react";
import { InputBar } from "../components/InputBar.js";
import { Layout } from "../components/Layout.js";
import { OutputPane } from "../components/OutputPane.js";
import { stubExecutor } from "../executor.js";
import {
  createServerBootstrapController,
  fetchStageBootstrap,
} from "../serverApi.js";
import type {
  BreadcrumbSegment,
  CommandOption,
  CommandSubmission,
  Executor,
  ExecutionRecord,
  ServerHealthStatus,
  ServerStatus,
  SystemAdmonition,
  UserSummary,
  WorkspaceSummary,
  WorkspaceUser,
} from "../types.js";

const MAX_EXECUTIONS = 100;
let executionSequence = 0;

const COMMAND_OPTIONS: CommandOption[] = [
  {
    value: "show",
    label: "show",
    description: "Browse the current node.",
  },
  {
    value: "describe",
    label: "describe",
    description: "Summarize metadata and shape.",
  },
  {
    value: "trace",
    label: "trace",
    description: "Follow lineage and upstream steps.",
  },
  {
    value: "open",
    label: "open",
    description: "Jump into the selected artifact.",
  },
];

const BREADCRUMBS: BreadcrumbSegment[] = [
  { label: "projects", href: "#projects" },
  { label: "omega", href: "#omega" },
  { label: "runs", href: "#runs" },
  { label: "run_42", href: "#run-42" },
  { label: "artifacts" },
];

const DEFAULT_CURRENT_USER = "mav";
const LOCATION_COMMANDS = new Set(["show"]);

const FALLBACK_USER: UserSummary = {
  name: DEFAULT_CURRENT_USER,
  role: "Workspace user",
  initials: "MV",
};

function createExecutionId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  executionSequence += 1;
  return `execution-${Date.now()}-${executionSequence}`;
}

interface ReplPageProps {
  executor?: Executor;
}

function buildInitials(value: string): string {
  const parts = value
    .trim()
    .split(/\s+/)
    .filter(Boolean);

  if (parts.length === 0) return "??";
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }

  return `${parts[0]?.[0] ?? ""}${parts[1]?.[0] ?? ""}`.toUpperCase();
}

function formatUserRole(user: WorkspaceUser): string {
  const username = `@${user.name}`;
  const groups = user.groups?.filter(Boolean) ?? [];
  if (groups.length === 0) {
    return username;
  }

  return `${username} · ${groups.join(", ")}`;
}

function toUserSummary(user: WorkspaceUser | null): UserSummary {
  if (user === null) {
    return FALLBACK_USER;
  }

  const displayName = user.description?.trim() || user.name;
  return {
    name: displayName,
    role: formatUserRole(user),
    initials: buildInitials(displayName),
    avatarUrl: user.avatarUrl,
  };
}

function formatServerEndpoint(protocol: string, endpoint: ServerHealthStatus["http"]) {
  return `${protocol}://${endpoint.host}:${endpoint.port}`;
}

function buildServerConnectedAdmonition(
  health: ServerHealthStatus,
): SystemAdmonition {
  const restEndpoint = formatServerEndpoint("http", health.http);
  const lspTransport = health.lsp.transport ?? "tcp";
  const lspEndpoint = formatServerEndpoint(lspTransport, health.lsp);

  return {
    id: "server-connected",
    tone: "gray",
    title: "mlody server connected",
    message:
      `Everything is good. REST ${restEndpoint} · ` +
      `LSP ${lspEndpoint}.`,
  };
}

export function ReplPage({ executor = stubExecutor }: ReplPageProps) {
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);
  const [currentCommand, setCurrentCommand] = useState("show");
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null>(null);
  const [availableUsers, setAvailableUsers] = useState<WorkspaceUser[]>([]);
  const [currentUserName] = useState(DEFAULT_CURRENT_USER);
  const [serverStatus, setServerStatus] = useState<ServerStatus>("connecting");
  const [admonitions, setAdmonitions] = useState<SystemAdmonition[]>([
    {
      id: "server-connecting",
      tone: "gray",
      title: "Connecting to mlody server",
      message:
        "Trying to load workspace context, available users, and system metadata.",
    },
  ]);

  useEffect(() => {
    const { controller, timeoutId } = createServerBootstrapController();
    let active = true;

    void fetchStageBootstrap(controller.signal)
      .then((payload) => {
        if (!active) return;
        setWorkspace(payload.workspace);
        setAvailableUsers(payload.users);
        setServerStatus("connected");
        setAdmonitions([buildServerConnectedAdmonition(payload.health)]);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const message =
          error instanceof Error
            ? error.message
            : "Unable to reach the mlody server.";
        setWorkspace(null);
        setAvailableUsers([]);
        setServerStatus("unavailable");
        setAdmonitions([
          {
            id: "server-unavailable",
            tone: "red",
            title: "mlody server unavailable",
            message:
              `Stage could not load workspace or user data. ` +
              `Checked the default server and got: ${message}`,
          },
        ]);
      })
      .finally(() => {
        window.clearTimeout(timeoutId);
      });

    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeoutId);
    };
  }, []);

  const currentWorkspaceUser =
    availableUsers.find((user) => user.name === currentUserName) ?? null;
  const currentUser = toUserSummary(currentWorkspaceUser);
  const showLocation = LOCATION_COMMANDS.has(currentCommand);

  const handleSubmit = ({ command, input }: CommandSubmission) => {
    const combinedCommand = [command, input].filter(Boolean).join(" ").trim();
    if (combinedCommand === "") return;

    const record: ExecutionRecord = {
      id: createExecutionId(),
      command: combinedCommand,
      submittedAt: new Date().toISOString(),
      status: "running",
      output: [],
    };

    setExecutions((prev) => [...prev, record].slice(-MAX_EXECUTIONS));

    void Promise.resolve()
      .then(() =>
        executor.run(combinedCommand, (chunk) => {
          setExecutions((prev) =>
            prev.map((r) =>
              r.id === record.id ? { ...r, output: [...r.output, chunk] } : r,
            ),
          );
        }),
      )
      .then(() => {
        setExecutions((prev) =>
          prev.map((r) =>
            r.id === record.id ? { ...r, status: "done" } : r,
          ),
        );
      })
      .catch((err: unknown) => {
        const message =
          err instanceof Error ? err.message : "Unknown error occurred";
        setExecutions((prev) =>
          prev.map((r) =>
            r.id === record.id
              ? {
                  ...r,
                  status: "error",
                  output: [
                    ...r.output,
                    { text: message, kind: "stderr" },
                  ],
                }
              : r,
          ),
        );
      });
  };

  return (
    <Layout>
      <OutputPane executions={executions} admonitions={admonitions} />
      <InputBar
        commandOptions={COMMAND_OPTIONS}
        currentCommand={currentCommand}
        breadcrumbs={BREADCRUMBS}
        workspace={workspace}
        showLocation={showLocation}
        currentUser={currentUser}
        onCommandChange={setCurrentCommand}
        onSubmit={handleSubmit}
        disabled={serverStatus === "connecting"}
      />
    </Layout>
  );
}
