export interface StageViewColumn {
  key: string;
  label: string;
  format?: string;
  display?: "image" | "badge-list";
}

export interface StageDagPort {
  id: string;
  label: string;
  side: "input" | "output";
  kind: "input" | "config" | "output" | "value";
  typeLabel?: string;
}

export interface StageDagNode {
  id: string;
  kind: "task" | "value";
  title: string;
  subtitle?: string | null;
  address?: string;
  position: {
    x: number;
    y: number;
  };
  ports: StageDagPort[];
}

export interface StageDagEdge {
  id: string;
  sourceNodeId: string;
  sourcePortId: string;
  targetNodeId: string;
  targetPortId: string;
  label?: string;
}

export interface StageDagData {
  nodes: StageDagNode[];
  edges: StageDagEdge[];
}

export interface StageEncodedImageCell {
  kind: "encoded-image";
  mimeType: string;
  base64: string;
  byteLength?: number;
  path?: string;
}

export type StageLineageSource = "default" | "config" | "user" | "task";

export interface StageLineageRow {
  source: StageLineageSource;
  value: unknown;
  active: boolean;
}

export interface StageSourceCodeData {
  path: string;
  language: string;
  startLine: number;
  endLine: number;
  code: string;
}

export interface StageResultPayload {
  kind: string;
  view: {
    type: string;
    title?: string;
    columns?: StageViewColumn[];
    rowCount?: number;
    truncated?: boolean;
    nodeCount?: number;
    edgeCount?: number;
  };
  data: unknown;
}

export type StageAutocompleteCompletionKind =
  | "root"
  | "folder"
  | "source_file"
  | "entity"
  | "field";

export interface StageAutocompleteCompletion {
  label: string;
  kind: StageAutocompleteCompletionKind;
}

export interface StageAutocompletePayload {
  completions: StageAutocompleteCompletion[];
  additionalData: Record<string, unknown>;
}

/** One line / chunk of output from an execution. */
export type OutputChunk =
  | {
      text: string;
      /** "stdout" for normal output, "stderr" for errors, "meta" for UI messages */
      kind: "stdout" | "stderr" | "meta";
    }
  | {
      kind: "stage-json";
      value: StageResultPayload;
    };

export type LocationPieceKind =
  | "entity"
  | "mlody-folder"
  | "mlody-source"
  | "wildcard"
  | "query";

export interface LocationPiece {
  kind: LocationPieceKind;
  text: string;
}

export interface LocationCrumb {
  id: string;
  href?: string;
  pieces: LocationPiece[];
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
  currentUserName: string;
  workspace: WorkspaceSummary | null;
}

/** Represents one submitted command and its execution state. */
export interface ExecutionRecord {
  id: string; // browser-safe unique id
  command: string;
  commandName: string;
  commandInput: string;
  runAs: string;
  workspaceRoot: string | null;
  /** ISO timestamp when the command was submitted */
  submittedAt: string;
  status: "running" | "done" | "error";
  output: OutputChunk[];
}

/** Callback type used by the executor to stream output chunks */
export type OutputCallback = (chunk: OutputChunk) => void;

export type ExecutionResultStatus = "done" | "error";

/** The executor abstraction — swap stub for real backend without touching UI */
export interface Executor {
  run(
    command: string,
    currentUserName: string,
    workspaceRoot: string | null,
    onChunk: OutputCallback,
  ): Promise<ExecutionResultStatus>;
}
