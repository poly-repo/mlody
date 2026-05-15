import type { CommandHistoryEntry } from "../commandHistory.js";
import { useEffect, useState } from "react";
import { InputBar } from "../components/InputBar.js";
import { Layout } from "../components/Layout.js";
import { OutputPane } from "../components/OutputPane.js";
import { runStageCommand, serverExecutor } from "../executor.js";
import {
  getStageE2eScenario,
  LAUNCH_WORKSPACE_ROOT,
  listStageE2eScenarioNames,
  resolveStageE2eWorkspaceRoot,
} from "../e2eTests.js";
import { parseStagePromptCommand } from "../promptCommands.js";
import {
  createServerBootstrapController,
  fetchStageBootstrap,
} from "../serverApi.js";
import type {
  CommandOption,
  CommandSubmission,
  Executor,
  ExecutionRecord,
  LocationCrumb,
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

const INITIAL_LOCATION: LocationCrumb[] = [];

const DEFAULT_CURRENT_USER = "mav";
const LOCATION_COMMANDS = new Set(["show"]);
const INITIAL_TOPDIR = "";

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

function buildFallbackUser(name: string): UserSummary {
  return {
    name,
    role: "Workspace user",
    initials: buildInitials(name),
  };
}

function toUserSummary(user: WorkspaceUser | null, fallbackName: string): UserSummary {
  if (user === null) {
    return buildFallbackUser(fallbackName);
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

function normalizePath(value: string): string {
  return value.replace(/\/+$/, "");
}

function sameWorkspaceRoot(
  left: WorkspaceSummary | null,
  right: WorkspaceSummary | null,
): boolean {
  if (left === null || right === null) {
    return left === right;
  }

  return normalizePath(left.workspaceRoot) === normalizePath(right.workspaceRoot);
}

function getWorkspaceTopdir(workspace: WorkspaceSummary | null): string {
  if (!workspace) {
    return INITIAL_TOPDIR;
  }

  const monorepoRoot = normalizePath(workspace.monorepoRoot);
  const workspaceRoot = normalizePath(workspace.workspaceRoot);

  if (workspaceRoot === monorepoRoot) {
    return "/";
  }

  if (workspaceRoot.startsWith(`${monorepoRoot}/`)) {
    return workspaceRoot.slice(monorepoRoot.length + 1);
  }

  return workspace.workspaceRoot;
}

export function ReplPage({ executor = serverExecutor }: ReplPageProps) {
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);
  const [currentCommand, setCurrentCommand] = useState("show");
  const [location] = useState<LocationCrumb[]>(INITIAL_LOCATION);
  const [bootstrapWorkspace, setBootstrapWorkspace] =
    useState<WorkspaceSummary | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null>(null);
  const [availableWorkspaces, setAvailableWorkspaces] = useState<
    WorkspaceSummary[]
  >([]);
  const [availableUsers, setAvailableUsers] = useState<WorkspaceUser[]>([]);
  const [currentUserName, setCurrentUserName] = useState(DEFAULT_CURRENT_USER);
  const [primedHistoryEntries, setPrimedHistoryEntries] = useState<
    CommandHistoryEntry[] | null
  >(null);
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
        const workspaces = payload.workspaces.some((candidate) =>
          sameWorkspaceRoot(candidate, payload.workspace),
        )
          ? payload.workspaces
          : [payload.workspace, ...payload.workspaces];
        setBootstrapWorkspace(payload.workspace);
        setWorkspace(payload.workspace);
        setAvailableWorkspaces(workspaces);
        setAvailableUsers(payload.users);
        setPrimedHistoryEntries(payload.history);
        setServerStatus("connected");
        setAdmonitions([buildServerConnectedAdmonition(payload.health)]);
      })
      .catch((error: unknown) => {
        if (!active) return;
        const message =
          error instanceof Error
            ? error.message
            : "Unable to reach the mlody server.";
        setBootstrapWorkspace(null);
        setWorkspace(null);
        setAvailableWorkspaces([]);
        setAvailableUsers([]);
        setPrimedHistoryEntries(null);
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
  const currentUser = toUserSummary(currentWorkspaceUser, currentUserName);
  const showLocation = LOCATION_COMMANDS.has(currentCommand);
  const topdir = getWorkspaceTopdir(workspace);

  function appendExecutionRecord(record: ExecutionRecord) {
    setExecutions((prev) => [...prev, record].slice(-MAX_EXECUTIONS));
  }

  function appendExecutionChunk(recordId: string, chunk: ExecutionRecord["output"][number]) {
    setExecutions((prev) =>
      prev.map((record) =>
        record.id === recordId
          ? { ...record, output: [...record.output, chunk] }
          : record,
      ),
    );
  }

  function setExecutionStatus(
    recordId: string,
    status: ExecutionRecord["status"],
  ) {
    setExecutions((prev) =>
      prev.map((record) =>
        record.id === recordId ? { ...record, status } : record,
      ),
    );
  }

  function setExecutionError(recordId: string, message: string) {
    setExecutions((prev) =>
      prev.map((record) =>
        record.id === recordId
          ? {
              ...record,
              status: "error",
              output: [
                ...record.output,
                { text: message, kind: "stderr" },
              ],
            }
          : record,
      ),
    );
  }

  function queueExecution(
    record: ExecutionRecord,
    runner: (
      onChunk: (chunk: ExecutionRecord["output"][number]) => void,
    ) => Promise<"done" | "error">,
  ) {
    appendExecutionRecord(record);

    void Promise.resolve()
      .then(() => runner((chunk) => appendExecutionChunk(record.id, chunk)))
      .then((status) => {
        setExecutionStatus(record.id, status);
      })
      .catch((err: unknown) => {
        const message =
          err instanceof Error ? err.message : "Unknown error occurred";
        setExecutionError(record.id, message);
      });
  }

  async function runNamedE2eScenario(
    scenarioName: string,
    onChunk: (chunk: ExecutionRecord["output"][number]) => void,
  ): Promise<"done" | "error"> {
    const trimmedScenarioName = scenarioName.trim();
    if (trimmedScenarioName === "") {
      throw new Error(
        `Missing e2e scenario name. Available tests: ${listStageE2eScenarioNames().join(", ")}`,
      );
    }

    const scenario = getStageE2eScenario(trimmedScenarioName);
    if (scenario === null) {
      throw new Error(
        `Unknown e2e scenario '${trimmedScenarioName}'. Available tests: ${listStageE2eScenarioNames().join(", ")}`,
      );
    }

    onChunk({
      kind: "meta",
      text:
        `Running e2e scenario '${scenario.name}' ` +
        `(${scenario.commands.length} show command${scenario.commands.length === 1 ? "" : "s"}).`,
    });

    for (const [index, [userName, workspaceTarget, label]] of scenario.commands.entries()) {
      const resolvedWorkspaceRoot = resolveStageE2eWorkspaceRoot(
        workspaceTarget,
        bootstrapWorkspace,
      );

      if (
        workspaceTarget === LAUNCH_WORKSPACE_ROOT &&
        resolvedWorkspaceRoot === null
      ) {
        throw new Error(
          "The e2e scenario requires the launch workspace root, but stage has not loaded workspace metadata yet.",
        );
      }

      const workspaceLabel =
        resolvedWorkspaceRoot ?? "(default workspace)";
      onChunk({
        kind: "meta",
        text:
          `[${index + 1}/${scenario.commands.length}] ` +
          `show ${label} · as ${userName} · workspace ${workspaceLabel}`,
      });

      await runStageCommand(
        "show",
        label,
        userName,
        resolvedWorkspaceRoot,
        onChunk,
      );
    }

    onChunk({
      kind: "meta",
      text: `Scenario '${scenario.name}' completed successfully.`,
    });
    return "done";
  }

  const handleSubmit = ({
    command,
    input,
    currentUserName: submittedUserName,
    workspace: submittedWorkspace,
  }: CommandSubmission) => {
    const parsedPromptCommand = parseStagePromptCommand(input);
    if (parsedPromptCommand.kind === "invalid") {
      queueExecution(
        {
          id: createExecutionId(),
          command: parsedPromptCommand.raw,
          commandName: parsedPromptCommand.raw,
          commandInput: "",
          copyCommand: null,
          runAs: submittedUserName,
          workspaceRoot: submittedWorkspace?.workspaceRoot ?? null,
          submittedAt: new Date().toISOString(),
          status: "running",
          output: [],
        },
        async () => {
          throw new Error(parsedPromptCommand.message);
        },
      );
      return;
    }

    if (parsedPromptCommand.kind === "command") {
      const commandName = parsedPromptCommand.name.trim();
      if (commandName === "e2e") {
        queueExecution(
          {
            id: createExecutionId(),
            command: parsedPromptCommand.raw,
            commandName: ",e2e",
            commandInput: parsedPromptCommand.args,
            copyCommand: null,
            runAs: submittedUserName,
            workspaceRoot: submittedWorkspace?.workspaceRoot ?? null,
            submittedAt: new Date().toISOString(),
            status: "running",
            output: [],
          },
          async (onChunk) =>
            await runNamedE2eScenario(parsedPromptCommand.args, onChunk),
        );
        return;
      }

      queueExecution(
        {
          id: createExecutionId(),
          command: parsedPromptCommand.raw,
          commandName: parsedPromptCommand.raw,
          commandInput: "",
          copyCommand: null,
          runAs: submittedUserName,
          workspaceRoot: submittedWorkspace?.workspaceRoot ?? null,
          submittedAt: new Date().toISOString(),
          status: "running",
          output: [],
        },
        async () => {
          throw new Error(
            `Unknown stage command ',${commandName}'. Currently supported: ,e2e.`,
          );
        },
      );
      return;
    }

    const combinedCommand = [command, input].filter(Boolean).join(" ").trim();
    if (combinedCommand === "") return;

    queueExecution(
      {
        id: createExecutionId(),
        command: combinedCommand,
        commandName: command,
        commandInput: input,
        runAs: submittedUserName,
        workspaceRoot: submittedWorkspace?.workspaceRoot ?? null,
        submittedAt: new Date().toISOString(),
        status: "running",
        output: [],
      },
      async (onChunk) =>
        await executor.run(
          combinedCommand,
          submittedUserName,
          submittedWorkspace?.workspaceRoot ?? null,
          onChunk,
        ),
    );
  };

  return (
    <Layout>
      <OutputPane executions={executions} admonitions={admonitions} />
      <InputBar
        commandOptions={COMMAND_OPTIONS}
        currentCommand={currentCommand}
        location={location}
        topdir={topdir}
        availableWorkspaces={availableWorkspaces}
        workspace={workspace}
        showLocation={showLocation}
        availableUsers={availableUsers}
        currentUserName={currentUserName}
        currentUser={currentUser}
        primedHistoryEntries={primedHistoryEntries}
        onCommandChange={setCurrentCommand}
        onCurrentUserChange={setCurrentUserName}
        onWorkspaceChange={setWorkspace}
        onSubmit={handleSubmit}
        disabled={serverStatus === "connecting"}
      />
    </Layout>
  );
}
