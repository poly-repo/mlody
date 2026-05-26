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

export interface StageActionGraphNode {
  id: string;
  kind: "task" | "value" | "resolve" | "prepare" | "action";
  title: string;
  subtitle?: string | null;
  description?: string | null;
  executor: string;
  executorDetail?: string | null;
  operation: string;
  structuralNodeId?: string | null;
  position: {
    x: number;
    y: number;
  };
}

export interface StageActionGraphEdge {
  id: string;
  sourceNodeId: string;
  targetNodeId: string;
}

export interface StageActionGraphData {
  nodes: StageActionGraphNode[];
  edges: StageActionGraphEdge[];
}

export interface StageEncodedImageCell {
  kind: "encoded-image";
  mimeType: string;
  base64: string;
  byteLength?: number;
  path?: string;
}

export interface StageLineageRow {
  source: string;
  value: unknown;
  details?: unknown;
  active: boolean;
}

export interface StageSummaryDetail {
  name: string;
  value: string;
}

export interface StageEntityValue {
  name: string;
  type: string;
  description: string;
  details: StageSummaryDetail[];
  detailsText: string;
}

export interface StageEntityAttribute {
  name: string;
  value: string;
  details: StageSummaryDetail[];
  detailsText: string;
}

export interface StageEntitySection {
  key: string;
  label: string;
  values: StageEntityValue[];
}

export interface StageEntityData {
  kind: string;
  name: string;
  description: string;
  attributes: StageEntityAttribute[];
  sections: StageEntitySection[];
  inputs: StageEntityValue[];
  outputs: StageEntityValue[];
  config: StageEntityValue[];
}

export type StageTaskPort = StageEntityValue;
export type StageTaskAttributeDetail = StageSummaryDetail;
export type StageTaskAttribute = StageEntityAttribute;
export type StageTaskData = StageEntityData;

export interface StageSourceCodeData {
  path: string;
  language: string;
  startLine: number;
  endLine: number;
  code: string;
}

export interface StageCommandLogEvent {
  event?: string;
  kind?: string;
  requestId?: string;
  timestamp?: string;
  [key: string]: unknown;
}

export interface StageCommandLogsPayload {
  requestId: string;
  events: StageCommandLogEvent[];
}

export interface StageValueType {
  kind?: string;
  name?: string;
  type?: string;
  _root_kind?: string;
  attributes?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StageResultPayload {
  kind: string;
  requestId?: string;
  view: {
    type: string;
    title?: string;
    entity?: string;
    columns?: StageViewColumn[];
    rowCount?: number;
    truncated?: boolean;
    nodeCount?: number;
    edgeCount?: number;
  };
  data: unknown;
  valueType?: StageValueType | null;
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

export interface KvEntry {
  key: string;
  value: string | null;
}

/** One line / chunk of output from an execution. */
export type OutputChunk =
  | {
      text: string;
      /** "stdout" for normal output, "stderr" for errors, "meta" for UI messages */
      kind: "stdout" | "stderr" | "meta";
    }
  | {
      /** Compact key-value row with styled keys and plain values. */
      kind: "kv";
      entries: KvEntry[];
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
  /**
   * Path of the workspace root relative to the server's monorepo root.
   * Empty string denotes the monorepo root itself.
   */
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
  instanceId: string;
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
  copyCommand?: string | null;
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
