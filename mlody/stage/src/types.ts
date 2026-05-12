/** One line / chunk of output from an execution. */
export interface OutputChunk {
  text: string;
  /** "stdout" for normal output, "stderr" for errors, "meta" for UI messages */
  kind: "stdout" | "stderr" | "meta";
}

export interface BreadcrumbSegment {
  label: string;
  href?: string;
}

export interface CommandOption {
  value: string;
  label: string;
  description?: string;
}

export interface UserSummary {
  name: string;
  role: string;
  initials: string;
  avatarUrl?: string;
}

export interface WorkspaceUser {
  name: string;
  description?: string;
  groups?: string[];
  avatar?: string;
  avatarUrl?: string;
}

export interface WorkspaceRootInfo {
  name: string;
  path: string;
  description: string;
}

export interface WorkspaceSummary {
  monorepoRoot: string;
  workspaceRoot: string;
  rootsFile: string | null;
  fullWorkspace: boolean;
  info?: Record<string, unknown> | null;
  rootInfos: WorkspaceRootInfo[];
  context?: {
    workspace?: Record<string, unknown>;
    run?: Record<string, unknown>;
  };
}

export interface ServerEndpointBinding {
  host: string;
  port: number;
  transport?: string;
}

export interface ServerHealthStatus {
  status: string;
  http: ServerEndpointBinding;
  lsp: ServerEndpointBinding;
}

export type ServerStatus = "connecting" | "connected" | "unavailable";

export type SystemAdmonitionTone =
  | "red"
  | "green"
  | "yellow"
  | "black"
  | "gray";

export interface SystemAdmonition {
  id: string;
  tone: SystemAdmonitionTone;
  title: string;
  message: string;
}

export interface CommandSubmission {
  command: string;
  input: string;
}

/** Represents one submitted command and its execution state. */
export interface ExecutionRecord {
  id: string; // browser-safe unique id
  command: string;
  /** ISO timestamp when the command was submitted */
  submittedAt: string;
  status: "running" | "done" | "error";
  output: OutputChunk[];
}

/** Callback type used by the executor to stream output chunks */
export type OutputCallback = (chunk: OutputChunk) => void;

/** The executor abstraction — swap stub for real backend without touching UI */
export interface Executor {
  run(command: string, onChunk: OutputCallback): Promise<void>;
}
