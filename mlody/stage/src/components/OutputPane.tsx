import { useEffect, useRef } from "react";
import type { ExecutionRecord } from "../types.js";
import { ExecutionBlock } from "./ExecutionBlock.js";

interface OutputPaneProps {
  executions: ExecutionRecord[];
}

export function OutputPane({ executions }: OutputPaneProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Scroll to bottom only when a new execution is added (not on every chunk)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [executions.length]);

  return (
    <div className="OutputPane">
      {executions.length === 0 && (
        <p className="OutputPane-empty">
          Type a command below and press Enter to execute.
        </p>
      )}
      {executions.map((record) => (
        <ExecutionBlock key={record.id} record={record} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
