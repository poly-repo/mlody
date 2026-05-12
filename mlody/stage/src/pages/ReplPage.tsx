import { useState } from "react";
import { InputBar } from "../components/InputBar.js";
import { Layout } from "../components/Layout.js";
import { OutputPane } from "../components/OutputPane.js";
import { stubExecutor } from "../executor.js";
import type {
  BreadcrumbSegment,
  CommandOption,
  CommandSubmission,
  Executor,
  ExecutionRecord,
  UserSummary,
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

const BREADCRUMBS: BreadcrumbSegment[] = [
  { label: "projects", href: "#projects" },
  { label: "omega", href: "#omega" },
  { label: "runs", href: "#runs" },
  { label: "run_42", href: "#run-42" },
  { label: "artifacts" },
];

const CURRENT_USER: UserSummary = {
  name: "Maya Patel",
  role: "Workspace operator",
  initials: "MP",
};

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
  const [currentCommand, setCurrentCommand] = useState("show");

  const handleSubmit = ({ command, input }: CommandSubmission) => {
    const combinedCommand = [command, input].filter(Boolean).join(" ").trim();
    if (combinedCommand === "") return;

    const record: ExecutionRecord = {
      id: createExecutionId(),
      command: combinedCommand,
      submittedAt: new Date().toISOString(),
      status: "running",
      output: [],
    };

    setExecutions((prev) => [...prev, record].slice(-MAX_EXECUTIONS));

    void Promise.resolve()
      .then(() =>
        executor.run(combinedCommand, (chunk) => {
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
      <OutputPane executions={executions} />
      <InputBar
        commandOptions={COMMAND_OPTIONS}
        currentCommand={currentCommand}
        breadcrumbs={BREADCRUMBS}
        currentUser={CURRENT_USER}
        onCommandChange={setCurrentCommand}
        onSubmit={handleSubmit}
      />
    </Layout>
  );
}
