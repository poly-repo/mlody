import { listStageE2eScenarioNames } from "./e2eTests.js";

export type StagePromptCommandParseResult =
  | { kind: "plain" }
  | {
      kind: "command";
      raw: string;
      name: string;
      args: string;
    }
  | {
      kind: "invalid";
      raw: string;
      message: string;
    };

const STAGE_PROMPT_COMMAND_PATTERN = /^,([^\s]+)(?:\s+([\s\S]*))?$/;
const STAGE_PROMPT_COMMAND_NAME_PATTERN = /^,[^\s]*$/;
const STAGE_PROMPT_COMMAND_VALID_FOR = /^[A-Za-z0-9_-]*$/;

export interface StagePromptAutocompleteOption {
  label: string;
  detail?: string;
  applySuffix?: string;
  triggerCompletionAfterApply?: boolean;
}

export interface StagePromptAutocompleteResult {
  from: number;
  options: StagePromptAutocompleteOption[];
  validFor: RegExp;
}

interface StagePromptCommandDefinition {
  name: string;
  description: string;
  completeArgs?: (args: string) => StagePromptAutocompleteResult | null;
}

function completeE2eArgs(args: string): StagePromptAutocompleteResult | null {
  if (/\s/.test(args)) {
    return null;
  }

  return {
    from: ",e2e ".length,
    options: listStageE2eScenarioNames().map((name) => ({
      label: name,
      detail: "Named e2e scenario",
    })),
    validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
  };
}

const STAGE_PROMPT_COMMANDS: readonly StagePromptCommandDefinition[] = [
  {
    name: "e2e",
    description: "Run a named client-side end-to-end scenario.",
    completeArgs: completeE2eArgs,
  },
];

function getStagePromptCommandDefinition(
  name: string,
): StagePromptCommandDefinition | null {
  return STAGE_PROMPT_COMMANDS.find((definition) => definition.name === name) ?? null;
}

export function listStagePromptCommandNames(): string[] {
  return STAGE_PROMPT_COMMANDS.map((definition) => definition.name).sort(
    (left, right) => left.localeCompare(right),
  );
}

export function isStagePromptCommandName(name: string): boolean {
  return getStagePromptCommandDefinition(name) !== null;
}

export function parseStagePromptCommand(
  input: string,
): StagePromptCommandParseResult {
  const trimmed = input.trim();
  if (!trimmed.startsWith(",")) {
    return { kind: "plain" };
  }

  const match = trimmed.match(STAGE_PROMPT_COMMAND_PATTERN);
  if (!match) {
    return {
      kind: "invalid",
      raw: trimmed,
      message:
        "Stage commands must start with ',' immediately followed by the command name, for example ',e2e smoketest'.",
    };
  }

  return {
    kind: "command",
    raw: trimmed,
    name: match[1] ?? "",
    args: match[2]?.trim() ?? "",
  };
}

export function analyzeStagePromptAutocomplete(
  input: string,
): StagePromptAutocompleteResult | null {
  if (input === "" || !input.startsWith(",")) {
    return null;
  }

  if (STAGE_PROMPT_COMMAND_NAME_PATTERN.test(input)) {
    return {
      from: 1,
      options: STAGE_PROMPT_COMMANDS.map((definition) => ({
        label: definition.name,
        detail: definition.description,
        applySuffix: " ",
        triggerCompletionAfterApply: true,
      })),
      validFor: STAGE_PROMPT_COMMAND_VALID_FOR,
    };
  }

  const match = input.match(/^,([^\s]+)\s+([\s\S]*)$/);
  if (match === null) {
    return null;
  }

  const definition = getStagePromptCommandDefinition(match[1] ?? "");
  return definition?.completeArgs?.(match[2] ?? "") ?? null;
}
