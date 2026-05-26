import { useEffect, useMemo, useState } from "react";
import { Code2, Eye, ScrollText } from "lucide-react";
import { LuCheck, LuCopy } from "react-icons/lu";
import { fetchStageCommandLogs } from "../serverApi.js";
import type {
  ExecutionRecord,
  KvEntry,
  OutputChunk,
  StageCommandLogEvent,
  StageResultPayload,
} from "../types.js";
import {
  hasSpecializedStageRenderer,
  StageResultBlock,
  type StageResultViewMode,
} from "./StageResultBlock.js";
import { StageLogsBlock } from "./StageLogsBlock.js";

interface ExecutionBlockProps {
  record: ExecutionRecord;
}

const COPY_RESET_MS = 1800;
const COPY_COMMAND_PREFIX = "bazel run --config=silent //mlody/cli:mlody --";

type ExecutionViewMode = "result" | "json" | "logs";

type StageJsonOutputChunk = Extract<OutputChunk, { kind: "stage-json" }>;

interface StageLogsLoadState {
  status: "loading" | "loaded" | "error";
  events: StageCommandLogEvent[];
  error?: string;
}

interface ExecutionViewOption {
  mode: ExecutionViewMode;
  label: string;
  icon: typeof Eye;
}

function formatTimestamp(isoString: string): string {
  const date = new Date(isoString);
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const ss = String(date.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\"'\"'`)}'`;
}

function buildCopyCommand(record: ExecutionRecord): string {
  const segments = [COPY_COMMAND_PREFIX];
  if (record.workspaceRoot !== null && record.workspaceRoot !== "") {
    segments.push(`--workspace ${shellQuote(record.workspaceRoot)}`);
  }
  segments.push(record.commandName);
  if (record.commandInput.trim() !== "") {
    segments.push(shellQuote(record.commandInput));
  }
  segments.push(`--as ${shellQuote(record.runAs)}`);
  return segments.join(" ");
}

function KvLine({ entries }: { entries: KvEntry[] }) {
  return (
    <span className="ExecutionBlock-kvLine">
      {entries.map((e, i) => (
        <span key={i} className="ExecutionBlock-kvEntry">
          <span className="ExecutionBlock-kvKey">{e.key}</span>
          {e.value !== null && (
            <span className="ExecutionBlock-kvValue">{e.value}</span>
          )}
        </span>
      ))}
    </span>
  );
}

interface StatusIconProps {
  className: string;
  label: string;
  children: React.ReactNode;
}

function StatusIcon({ className, label, children }: StatusIconProps) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      aria-label={label}
    >
      {children}
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <StatusIcon className="ExecutionBlock-spinner" label="Running">
      <circle cx="12" cy="12" r="10" strokeOpacity="0.25" />
      <path d="M12 2a10 10 0 0 1 10 10" />
    </StatusIcon>
  );
}

function CheckIcon() {
  return (
    <StatusIcon className="ExecutionBlock-done" label="Done">
      <polyline points="20 6 9 17 4 12" />
    </StatusIcon>
  );
}

function ErrorIcon() {
  return (
    <StatusIcon className="ExecutionBlock-error" label="Error">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </StatusIcon>
  );
}

function isStageJsonChunk(chunk: OutputChunk): chunk is StageJsonOutputChunk {
  return chunk.kind === "stage-json";
}

function hasStageRequestId(
  payload: StageResultPayload,
): payload is StageResultPayload & { requestId: string } {
  return typeof payload.requestId === "string" && payload.requestId.trim() !== "";
}

export function ExecutionBlock({ record }: ExecutionBlockProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">(
    "idle",
  );
  const [selectedView, setSelectedView] = useState<ExecutionViewMode>("result");
  const [logStates, setLogStates] = useState<Record<string, StageLogsLoadState>>(
    {},
  );
  const copyCommand =
    record.copyCommand === undefined ? buildCopyCommand(record) : record.copyCommand;
  const copyButtonSubject =
    record.copyCommand === undefined ? "full bazel run command" : "command";
  const stageJsonChunks = record.output.filter(isStageJsonChunk);
  const hasSpecializedStageOutput = stageJsonChunks.some((chunk) =>
    hasSpecializedStageRenderer(chunk.value),
  );
  const stageLogRequests = stageJsonChunks.flatMap((chunk) =>
    hasStageRequestId(chunk.value)
      ? [
          {
            requestId: chunk.value.requestId,
            title: chunk.value.view.title,
          },
        ]
      : [],
  );
  const stageLogRequestIds = Array.from(
    new Set(stageLogRequests.map((request) => request.requestId)),
  );
  const stageLogRequestKey = stageLogRequestIds.join("\u0000");
  const availableViews = useMemo<ExecutionViewOption[]>(() => {
    const views: ExecutionViewOption[] = [
      {
        mode: "result",
        label: "Result",
        icon: Eye,
      },
    ];
    if (hasSpecializedStageOutput) {
      views.push({
        mode: "json",
        label: "JSON",
        icon: Code2,
      });
    }
    if (stageLogRequestIds.length > 0) {
      views.push({
        mode: "logs",
        label: "Logs",
        icon: ScrollText,
      });
    }
    return views;
  }, [hasSpecializedStageOutput, stageLogRequestIds.length]);
  const copyButtonLabel =
    copyState === "copied"
      ? `Copied ${copyButtonSubject}`
      : copyState === "error"
      ? `Copy ${copyButtonSubject} failed`
      : `Copy ${copyButtonSubject}`;

  useEffect(() => {
    if (copyState === "idle") {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      setCopyState("idle");
    }, COPY_RESET_MS);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [copyState]);

  useEffect(() => {
    if (!availableViews.some((view) => view.mode === selectedView)) {
      setSelectedView("result");
    }
  }, [availableViews, selectedView]);

  useEffect(() => {
    if (selectedView !== "logs" || stageLogRequestIds.length === 0) {
      return;
    }

    const pendingRequestIds = stageLogRequestIds.filter((requestId) => {
      const state = logStates[requestId];
      return state === undefined;
    });
    if (pendingRequestIds.length === 0) {
      return;
    }

    const controllers = pendingRequestIds.map(() => new AbortController());

    setLogStates((currentStates) => {
      const nextStates = { ...currentStates };
      for (const requestId of pendingRequestIds) {
        if (nextStates[requestId] === undefined) {
          nextStates[requestId] = {
            status: "loading",
            events: [],
          };
        }
      }
      return nextStates;
    });

    pendingRequestIds.forEach((requestId, index) => {
      const controller = controllers[index];
      void fetchStageCommandLogs(requestId, controller.signal)
        .then((payload) => {
          setLogStates((currentStates) => ({
            ...currentStates,
            [requestId]: {
              status: "loaded",
              events: payload.events,
            },
          }));
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) {
            return;
          }
          setLogStates((currentStates) => ({
            ...currentStates,
            [requestId]: {
              status: "error",
              events: [],
              error:
                error instanceof Error ? error.message : "Failed to load stage logs.",
            },
          }));
        });
    });

    return () => {
      for (const controller of controllers) {
        controller.abort();
      }
    };
  }, [selectedView, stageLogRequestKey]);

  async function handleCopyClick() {
    if (copyCommand === null) {
      return;
    }

    try {
      await navigator.clipboard.writeText(copyCommand);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  const stageResultViewMode: StageResultViewMode =
    selectedView === "json" ? "json" : "rendered";

  return (
    <div className={`ExecutionBlock ExecutionBlock--${record.status}`}>
      <div className="ExecutionBlock-header">
        <span className="ExecutionBlock-timestamp">
          {formatTimestamp(record.submittedAt)}
        </span>
        <span className="ExecutionBlock-command" title={record.command}>
          {record.command}
        </span>
        {stageJsonChunks.length > 0 && availableViews.length > 1 ? (
          <div className="ExecutionBlock-viewSwitch" aria-label="Execution views">
            {availableViews.map((view) => {
              const Icon = view.icon;
              const isActive = selectedView === view.mode;
              return (
                <button
                  key={view.mode}
                  type="button"
                  className={`ExecutionBlock-viewButton${isActive ? " ExecutionBlock-viewButton--active" : ""}`}
                  aria-pressed={isActive}
                  onClick={() => {
                    setSelectedView(view.mode);
                  }}
                >
                  <Icon aria-hidden="true" />
                  <span>{view.label}</span>
                </button>
              );
            })}
          </div>
        ) : null}
        {copyCommand !== null ? (
          <button
            type="button"
            className={`ExecutionBlock-copyButton ExecutionBlock-copyButton--${copyState}`}
            aria-label={copyButtonLabel}
            title={copyCommand}
            onClick={() => {
              void handleCopyClick();
            }}
          >
            {copyState === "copied" ? (
              <LuCheck aria-hidden="true" />
            ) : (
              <LuCopy aria-hidden="true" />
            )}
          </button>
        ) : null}
        <span className="ExecutionBlock-status">
          {record.status === "running" && <SpinnerIcon />}
          {record.status === "done" && <CheckIcon />}
          {record.status === "error" && <ErrorIcon />}
        </span>
      </div>
      <div className="ExecutionBlock-body">
        {record.output.map((chunk, idx) => {
          if (chunk.kind === "kv") {
            return (
              <span
                key={idx}
                className="ExecutionBlock-line ExecutionBlock-line--kv"
              >
                <KvLine entries={chunk.entries} />
              </span>
            );
          }
          if (chunk.kind !== "stage-json") {
            return (
              <span
                key={idx}
                className={`ExecutionBlock-line ExecutionBlock-line--${chunk.kind}`}
              >
                {chunk.text}
              </span>
            );
          }

          if (selectedView === "logs") {
            if (!hasStageRequestId(chunk.value)) {
              return (
                <div
                  key={idx}
                  className="ExecutionBlock-line ExecutionBlock-line--stageJson"
                >
                  <div className="ExecutionBlock-logUnavailable">
                    Logs are unavailable for this result.
                  </div>
                </div>
              );
            }
            const logState = logStates[chunk.value.requestId];
            return (
              <div
                key={idx}
                className="ExecutionBlock-line ExecutionBlock-line--stageJson"
              >
                <StageLogsBlock
                  title={chunk.value.view.title}
                  requestId={chunk.value.requestId}
                  status={logState?.status ?? "loading"}
                  events={logState?.events}
                  error={logState?.error}
                />
              </div>
            );
          }

          return (
            <div
              key={idx}
              className="ExecutionBlock-line ExecutionBlock-line--stageJson"
            >
              <StageResultBlock payload={chunk.value} mode={stageResultViewMode} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
