import { useState } from "react";
import { GearIcon } from "../components/GearIcon.js";
import { InputBar } from "../components/InputBar.js";
import { Layout } from "../components/Layout.js";
import { OutputPane } from "../components/OutputPane.js";
import { stubExecutor } from "../executor.js";
import type { Executor, ExecutionRecord } from "../types.js";

interface ReplPageProps {
  executor?: Executor;
}

export function ReplPage({ executor = stubExecutor }: ReplPageProps) {
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);

  const handleSubmit = (command: string) => {
    //if (command.trim() === "") return;

    const record: ExecutionRecord = {
      id: crypto.randomUUID(),
      command,
      submittedAt: new Date().toISOString(),
      status: "running",
      output: [],
    };

    setExecutions((prev) => [...prev, record]);

    void executor
      .run(command, (chunk) => {
        setExecutions((prev) =>
          prev.map((r) =>
            r.id === record.id ? { ...r, output: [...r.output, chunk] } : r,
          ),
        );
      })
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
