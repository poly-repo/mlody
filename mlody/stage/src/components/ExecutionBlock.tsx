import { useEffect, useState } from "react";
import { LuCheck, LuCopy } from "react-icons/lu";
import type { ExecutionRecord } from "../types.js";
import { StageResultBlock } from "./StageResultBlock.js";

interface ExecutionBlockProps {
  record: ExecutionRecord;
}

const COPY_RESET_MS = 1800;
const COPY_COMMAND_PREFIX = "bazel run --config=silent //mlody/cli:mlody --";

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
  if (record.workspaceRoot !== null) {
    segments.push(`--workspace ${shellQuote(record.workspaceRoot)}`);
  }
  segments.push(record.commandName);
  if (record.commandInput.trim() !== "") {
    segments.push(shellQuote(record.commandInput));
  }
  segments.push(`--as ${shellQuote(record.runAs)}`);
  return segments.join(" ");
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

export function ExecutionBlock({ record }: ExecutionBlockProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">(
    "idle",
  );
  const copyCommand = buildCopyCommand(record);
  const copyButtonLabel =
    copyState === "copied"
      ? "Copied full bazel run command"
      : copyState === "error"
      ? "Copy full bazel run command failed"
      : "Copy full bazel run command";

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

  async function handleCopyClick() {
    try {
      await navigator.clipboard.writeText(copyCommand);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  return (
    <div className={`ExecutionBlock ExecutionBlock--${record.status}`}>
      <div className="ExecutionBlock-header">
        <span className="ExecutionBlock-timestamp">
          {formatTimestamp(record.submittedAt)}
        </span>
        <span className="ExecutionBlock-command" title={record.command}>
          {record.command}
        </span>
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
        <span className="ExecutionBlock-status">
          {record.status === "running" && <SpinnerIcon />}
          {record.status === "done" && <CheckIcon />}
          {record.status === "error" && <ErrorIcon />}
        </span>
      </div>
      <div className="ExecutionBlock-body">
        {record.output.map((chunk, idx) => (
          chunk.kind === "stage-json" ? (
            <div
              key={idx}
              className="ExecutionBlock-line ExecutionBlock-line--stageJson"
            >
              <StageResultBlock payload={chunk.value} />
            </div>
          ) : (
            <span
              key={idx}
              className={`ExecutionBlock-line ExecutionBlock-line--${chunk.kind}`}
            >
              {chunk.text}
            </span>
          )
        ))}
      </div>
    </div>
  );
}
