import { useState } from "react";
import { GearIcon } from "../components/GearIcon.js";
import { InputBar } from "../components/InputBar.js";
import { Layout } from "../components/Layout.js";
import { OutputPane } from "../components/OutputPane.js";
import { stubExecutor } from "../executor.js";
import type { Executor, ExecutionRecord } from "../types.js";

const MAX_EXECUTIONS = 100;
let executionSequence = 0;

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

export function ReplPage({ executor = stubExecutor }: ReplPageProps) {
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);

  const handleSubmit = (command: string) => {
    if (command === "") return;

    const record: ExecutionRecord = {
      id: createExecutionId(),
      command,
      submittedAt: new Date().toISOString(),
      status: "running",
      output: [],
    };

    setExecutions((prev) => [...prev, record].slice(-MAX_EXECUTIONS));

    void Promise.resolve()
      .then(() =>
        executor.run(command, (chunk) => {
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
      <GearIcon />
      <OutputPane executions={executions} />
      <InputBar onSubmit={handleSubmit} />
    </Layout>
  );
}
