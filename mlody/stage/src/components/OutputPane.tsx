import { useEffect, useRef } from "react";
import type { ExecutionRecord, SystemAdmonition } from "../types.js";
import { ExecutionBlock } from "./ExecutionBlock.js";
import { SystemAdmonitionBlock } from "./SystemAdmonitionBlock.js";

interface OutputPaneProps {
  executions: ExecutionRecord[];
  admonitions: SystemAdmonition[];
}

export function OutputPane({ executions, admonitions }: OutputPaneProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const noticeCountLabel =
    admonitions.length === 1
      ? "1 system notice"
      : `${admonitions.length} system notices`;

  // Scroll to bottom only when a new execution is added (not on every chunk)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [executions.length, admonitions.length]);

  return (
    <div className="OutputPane">
      <div className="OutputPane-header">
        <span className="OutputPane-eyebrow">Output</span>
        <span className="OutputPane-count">
          {executions.length === 0 && admonitions.length > 0
            ? noticeCountLabel
            : executions.length === 0
            ? "Waiting for your first command"
            : `${executions.length} execution${executions.length === 1 ? "" : "s"}`}
        </span>
      </div>
      <div className="OutputPane-feed">
        {admonitions.map((admonition) => (
          <SystemAdmonitionBlock key={admonition.id} admonition={admonition} />
        ))}
        {executions.length === 0 && admonitions.length === 0 && (
          <p className="OutputPane-empty">
            Choose a command, add a target or argument, and press Enter to
            execute.
          </p>
        )}
        {executions.map((record) => (
          <ExecutionBlock key={record.id} record={record} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
