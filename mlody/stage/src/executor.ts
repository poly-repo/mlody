import type { ExecutionResultStatus, Executor, OutputCallback } from "./types.js";
import { executeStageCommand } from "./serverApi.js";

function splitCommandInput(commandLine: string): {
  command: string;
  input: string;
} {
  const trimmed = commandLine.trim();
  const separatorIndex = trimmed.indexOf(" ");

  if (separatorIndex === -1) {
    return { command: trimmed, input: "" };
  }

  return {
    command: trimmed.slice(0, separatorIndex),
    input: trimmed.slice(separatorIndex + 1),
  };
}

export const serverExecutor: Executor = {
  async run(
    commandLine: string,
    currentUserName: string,
    workspaceRoot: string | null,
    onChunk: OutputCallback,
  ): Promise<ExecutionResultStatus> {
    const { command, input } = splitCommandInput(commandLine);
    const response = await executeStageCommand(
      command,
      input,
      currentUserName,
      workspaceRoot,
    );
    onChunk({ kind: "stage-json", value: response });

    return "done";
  },
};

export const stubExecutor: Executor = {
  async run(
    command: string,
    _currentUserName: string,
    _workspaceRoot: string | null,
    onChunk: OutputCallback,
  ): Promise<ExecutionResultStatus> {
    onChunk({
      kind: "stage-json",
      value: {
        kind: "result",
        view: {
          type: "json",
          title: "Stub result",
        },
        data: {
          command,
        },
      },
    });
    return "done";
  },
};
