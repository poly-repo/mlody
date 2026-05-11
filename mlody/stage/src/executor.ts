import type { Executor, OutputCallback } from "./types.js";

export const stubExecutor: Executor = {
  async run(command: string, onChunk: OutputCallback): Promise<void> {
    onChunk({ text: `> ${command}`, kind: "meta" });
    await new Promise((resolve) => setTimeout(resolve, 120));
    onChunk({ text: command, kind: "stdout" });
  },
};
